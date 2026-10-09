# -*- coding: utf-8 -*-
"""语音朗读模块 v9 —— 双引擎

1) NeuralSpeaker：edge-tts 网络神经网络语音（自然、逼真，需联网）。
   逐段生成 MP3，用 Windows MCI 播放，支持 暂停/继续/停止/段落进度。
2) SapiSpeaker：Windows 原生 SAPI（离线兜底，逻辑沿用 v8）。

Speaker 门面：
- engine_mode = 'neural' | 'sapi'
- AI 引擎不可用（组件缺失）时，自动退回本地 SAPI。
"""

import ctypes
import os
import queue
import re
import shutil
import tempfile
import threading
import time

from extract import clean_for_speech, split_chunks


# ---------- 多语言消息 ----------
SPK_MSG = {
    'zh': {
        'fallback': 'AI 网络语音中断，自动改用本地语音继续…',
        'net_fail': '网络语音失败（请检查网络），已停止',
        'paused': '已暂停（第 %d/%d 段）',
        'reading': '正在朗读第 %d/%d 段',
        'done': '朗读完成',
        'empty': '没有可朗读的内容',
        'unavailable': 'AI 语音组件不可用，请用本地语音',
        'preparing': '正在准备第 %d/%d 段…',
        'reading_local': '正在朗读第 %d/%d 段（本地语音）',
        'unknown_voice': '未知语音',
        'export_mp3': 'AI 网络语音已导出为 MP3',
        'export_fail': 'AI 导出失败（检查网络）：%s',
        'export_wav': '本地语音已导出为 WAV',
        'export_wav_fail': '本地语音导出失败：%s',
    },
    'en': {
        'fallback': 'AI network voice lost, continuing with local voice...',
        'net_fail': 'Network voice failed (check internet), stopped',
        'paused': 'Paused (segment %d/%d)',
        'reading': 'Reading segment %d/%d',
        'done': 'Finished reading',
        'empty': 'Nothing to read',
        'unavailable': 'AI voice component unavailable, use local voice',
        'preparing': 'Preparing segment %d/%d...',
        'reading_local': 'Reading segment %d/%d (local voice)',
        'unknown_voice': 'Unknown voice',
        'export_mp3': 'AI voice exported to MP3',
        'export_fail': 'AI export failed (check internet): %s',
        'export_wav': 'Local voice exported to WAV',
        'export_wav_fail': 'Local voice export failed: %s',
    },
}


# ---------- 通用工具 ----------

def _split_sentences(text):
    """按中文句末标点切句，保留能成句的最小单位；无标点长段按长度兜底切。"""
    parts = re.split(r'(?<=[。！？；…\n])', text)
    out, buf = [], ''
    for p in parts:
        if not p:
            continue
        buf += p
        if len(buf) >= 40 or p[-1:] in '。！？；…\n':
            out.append(buf.strip())
            buf = ''
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


def _edge_rate_str(mult):
    """合成倍速 → edge-tts 百分比。语音服务只支持 0.5x~2x（-50%~+100%）。"""
    m = max(0.5, min(2.0, mult))
    pct = int(round((m - 1) * 100))
    return ('%+d%%' % max(-50, min(100, pct)))


# 并发预合成窗口大小：同时最多几个 edge-tts 请求在飞。
# 实测（488 万字长篇，5 段共 1000 字）：串行 18.07 秒 vs 并发 1.75 秒，
# 5 并发已是安全值，再高容易被服务端限流。
SYNTH_PREFETCH = 5

# 音频头尾静音裁剪（实测数据，1.0 倍速，40 字一段）：
#   开头静音 0.100s、结尾静音 0.577s，合计约 0.68 秒；
#   两段相连 = 前段尾静音 + 后段头静音 ≈ 0.7 秒空白，
#   而倍速并不会按比例缩短它（1.5 倍时仍有 0.48 秒），
#   所以倍速越高越明显 —— 这就是"段落之间停顿难受"的真正来源。
# 做法：合成时顺带取逐词时间轴，播放时用 MCI 的 from/to 只播
#   [第一个词之前一点点, 最后一个词之后一点点]，
# 把没有意义的头尾静音丢掉，同时保留一点自然留白。
TRIM_LEAD_MARGIN_MS = 40    # 开头保留多少毫秒留白（别削到起音）
TRIM_TAIL_MARGIN_MS = 60    # 结尾保留多少毫秒留白（听感自然）


# ---------- Windows MCI 播放器（播放 MP3，支持暂停/继续/停止） ----------

class _MciPlayer:
    """用 winmm MCI 播放 MP3。兼容 Windows XP 及以上，无需额外组件。"""

    def __init__(self, alias='neu'):
        self.alias = alias
        self._winmm = ctypes.windll.winmm
        self._opened = False

    def _send(self, cmd):
        buf = ctypes.create_unicode_buffer(512)
        rc = self._winmm.mciSendStringW(cmd, buf, 512, None)
        if rc != 0:
            err = ctypes.create_unicode_buffer(512)
            self._winmm.mciGetErrorStringW(rc, err, 512)
            raise OSError(err.value or ('MCI error %d' % rc))
        return buf.value

    def open(self, path):
        self.close()
        self._send('open "%s" type mpegvideo alias %s' % (path, self.alias))
        self._opened = True

    def play(self):
        self._send('play %s' % self.alias)

    def set_time_ms(self):
        """把时间格式设为毫秒，from/to 才能按毫秒定位。"""
        try:
            self._send('set %s time format milliseconds' % self.alias)
        except Exception:
            pass

    def play_range(self, start_ms=0, end_ms=0):
        """只播放 [start_ms, end_ms] 区间；end_ms<=0 表示播到结尾。

        用来跳过 edge-tts 在每段音频头尾加的无意义静音。
        """
        try:
            start_ms = int(start_ms)
            end_ms = int(end_ms)
        except Exception:
            start_ms = end_ms = 0
        if start_ms > 0 and end_ms > start_ms:
            self._send('play %s from %d to %d'
                       % (self.alias, start_ms, end_ms))
        elif start_ms > 0:
            self._send('play %s from %d' % (self.alias, start_ms))
        elif end_ms > 0:
            self._send('play %s to %d' % (self.alias, end_ms))
        else:
            self._send('play %s' % self.alias)

    def pause(self):
        self._send('pause %s' % self.alias)

    def set_volume(self, vol):
        # MCI 音量范围是 0~1000；界面传进来的是 0~100，放大 10 倍才是真实音量
        v = int(max(0, min(1000, vol * 10)))
        self._send('setaudio %s volume to %d' % (self.alias, v))

    def resume(self):
        self._send('resume %s' % self.alias)

    def stop(self):
        if self._opened:
            try:
                self._send('stop %s' % self.alias)
            except Exception:
                pass

    def close(self):
        if self._opened:
            try:
                self._send('close %s' % self.alias)
            except Exception:
                pass
            self._opened = False

    def mode(self):
        try:
            return self._send('status %s mode' % self.alias).strip().lower()
        except Exception:
            return ''

    def position_ms(self):
        """当前播放位置（毫秒）；取不到返回 None。需先 set_time_ms()。"""
        try:
            v = self._send('status %s position' % self.alias)
            return int(float(str(v).strip() or 0))
        except Exception:
            return None


class _WmpPlayer:
    """备用播放器：Windows Media Player OCX（MCI 不可用时启用）。"""

    def __init__(self):
        self._player = None
        self._opened = False

    def _ensure(self):
        if self._player is None:
            import comtypes.client
            self._player = comtypes.client.CreateObject('WMPlayer.OCX')
        return self._player

    def open(self, path):
        p = self._ensure()
        self.close()
        p.currentMedia = p.newMedia(path)
        self._opened = True

    def play(self):
        self._ensure().controls.play()

    def play_range(self, start_ms=0, end_ms=0):
        """备用播放器不支持区间播放，退回整段播放（不影响朗读，只是不裁静音）。"""
        self.play()

    def set_rate(self, rate):
        """播放倍速（0.5x~2.0x），用于叠加出最高 4 倍速。"""
        try:
            self._ensure().settings.rate = float(max(0.5, min(2.0, rate)))
        except Exception:
            pass

    def set_volume(self, vol):
        try:
            self._ensure().settings.volume = int(max(0, min(100, vol)))
        except Exception:
            pass

    def pause(self):
        self._ensure().controls.pause()

    def resume(self):
        self._ensure().controls.play()

    def stop(self):
        if self._opened:
            try:
                self._ensure().controls.stop()
            except Exception:
                pass

    def close(self):
        if self._opened:
            try:
                self._ensure().controls.stop()
            except Exception:
                pass
            try:
                self._player.currentMedia = None
            except Exception:
                pass
            self._opened = False

    def mode(self):
        try:
            st = int(self._ensure().playState)
            # WMP 状态：1 停止 / 2 暂停 / 3 播放中 / 8 播放结束 / 10 就绪
            if st == 2:
                return 'paused'
            if st in (3, 4, 5, 6, 7, 9):
                return 'playing'
            return 'stopped'   # 0/1/8/10/11 都按已结束处理
        except Exception:
            return ''


class _AudioPlayer:
    """统一播放器：优先 MCI，失败自动改用 Windows Media Player。"""

    def __init__(self, alias='neu'):
        self._mci = _MciPlayer(alias)
        self._wmp = _WmpPlayer()
        self._backend = None

    def open(self, path):
        try:
            self._mci.open(path)
            self._backend = 'mci'
            return
        except Exception:
            pass
        try:
            self._wmp.open(path)
            self._backend = 'wmp'
            return
        except Exception:
            raise

    def play(self):
        if self._backend == 'mci':
            self._mci.play()
        else:
            self._wmp.play()

    def set_time_ms(self):
        if self._backend == 'mci':
            self._mci.set_time_ms()

    def play_range(self, start_ms=0, end_ms=0):
        """区间播放（用于裁掉音频头尾静音）。"""
        if self._backend == 'mci':
            self._mci.play_range(start_ms, end_ms)
        else:
            self._wmp.play_range(start_ms, end_ms)

    def pause(self):
        if self._backend == 'mci':
            self._mci.pause()
        else:
            self._wmp.pause()

    def resume(self):
        if self._backend == 'mci':
            try:
                self._mci.resume()
            except Exception:
                self._mci.play()   # 部分系统不支持 resume，用 play 续播
        else:
            self._wmp.resume()

    def set_playback_rate(self, rate):
        """播放层倍速：仅备用播放器支持（MCI 无变速能力，自动忽略）。"""
        if self._backend == 'wmp':
            self._wmp.set_rate(rate)

    def set_volume(self, vol):
        vol = int(max(0, min(100, vol)))
        if self._backend == 'wmp':
            self._wmp.set_volume(vol)
        elif self._backend == 'mci':
            try:
                self._mci.set_volume(vol)
            except Exception:
                pass

    def stop(self):
        self._mci.stop()
        self._wmp.stop()

    def close(self):
        self._mci.close()
        self._wmp.close()

    def mode(self):
        if self._backend == 'mci':
            return self._mci.mode()
        return self._wmp.mode()

    def position_ms(self):
        """当前播放位置（毫秒）；备用后端不支持则返回 None。"""
        if self._backend == 'mci':
            return self._mci.position_ms()
        return None


# ---------- 神经网络语音（edge-tts） ----------

NEURAL_VOICE_NAMES = {
    'zh-CN-XiaoxiaoNeural': '晓晓 · 女声（标准，推荐）',
    'zh-CN-XiaoyiNeural': '晓伊 · 女声',
    'zh-CN-YunjianNeural': '云健 · 男声',
    'zh-CN-YunxiNeural': '云希 · 男声（阳光）',
    'zh-CN-YunxiaNeural': '云夏 · 男声（少年）',
    'zh-CN-YunyangNeural': '云扬 · 男声（新闻）',
    'zh-CN-liaoning-XiaobeiNeural': '晓北 · 东北女声',
    'zh-CN-shaanxi-XiaoniNeural': '晓妮 · 陕西女声',
    'zh-HK-HiuGaaiNeural': '曉佳 · 粤语女声',
    'zh-HK-HiuMaanNeural': '曉曼 · 粤语女声',
    'zh-HK-WanLungNeural': '雲龍 · 粤语男声',
    'zh-TW-HsiaoChenNeural': '曉臻 · 台湾女声',
    'zh-TW-HsiaoYuNeural': '曉雨 · 台湾女声',
    'zh-TW-YunJheNeural': '雲哲 · 台湾男声',
    'en-US-AriaNeural': 'Aria · 英语女声',
    'en-US-GuyNeural': 'Guy · 英语男声',
    'en-US-JennyNeural': 'Jenny · 英语女声',
    'en-GB-SoniaNeural': 'Sonia · 英音女声',
}


class NeuralSpeaker:
    """AI 神经网络语音引擎：逐段合成 MP3，MCI 播放，支持进度/暂停/停止。"""

    def __init__(self):
        self._lang = 'zh'
        self._voice = 'zh-CN-XiaoxiaoNeural'
        self._rate_mult = 1.0
        self._play_rate = 1.0
        self._volume = 100
        self._volume_changed = False
        self._rate_ver = 0
        self._synth_gen = 0
        self._fallback = None
        self._stop_evt = threading.Event()
        self._paused_evt = threading.Event()
        self._thread = None
        self._cmdq = queue.Queue()
        self._lock = threading.Lock()
        self._player = _AudioPlayer('neu')
        self._pending = ([], None, None, 0)   # chunks, on_progress, on_state, offset
        self._cur_range = None    # 当前播放段的 (start_ms, end_ms)，用于算进度
        self._active_chunks = None
        self._active_idx = 0
        self._active_pb = None
        self._active_st = None

    @staticmethod
    def available():
        """组件是否可用（网络是否通由调用时的 list_voices 探测）。"""
        try:
            import edge_tts  # noqa: F401
            return True
        except Exception:
            return False

    def _T(self, key, *args):
        msg = SPK_MSG.get(self._lang, SPK_MSG['zh']).get(key, key)
        return msg % args if args else msg

    def set_language(self, lang):
        self._lang = 'en' if lang == 'en' else 'zh'

    # ---------- 声线 ----------
    def list_voices(self):
        try:
            import asyncio
            import edge_tts
            vs = asyncio.run(edge_tts.list_voices())
            out = []
            for v in vs:
                sid = v.get('ShortName') or v.get('Name') or ''
                if sid.startswith('zh-') or sid.startswith('en-'):
                    name = NEURAL_VOICE_NAMES.get(sid, v.get('FriendlyName') or sid)
                    out.append({'id': sid, 'name': name})
            return out
        except Exception:
            return []

    def set_voice(self, voice_id):
        self._voice = voice_id
        return True

    def current_voice_id(self):
        return self._voice

    # ---------- 倍速 ----------
    def set_rate(self, mult):
        """总倍速 0.5x~3.0x。
        ≤2x 全部用语音合成实现（音调自然）；>2x 合成按 2x + 播放器再加速。"""
        self._rate_mult = max(0.5, min(3.0, mult))
        if self._rate_mult <= 2.0:
            self._play_rate = 1.0
        else:
            self._play_rate = max(1.0, min(2.0, self._rate_mult / 2.0))
        with self._lock:
            self._rate_ver += 1

    def _rate_str(self):
        return _edge_rate_str(self._rate_mult)

    def _synth_one(self, text, idx, tmp):
        """按当前倍速合成单段 MP3；合成途中倍速变化会自动用新倍速重试。

        文件名带倍速版本号（0001_2.mp3）：倍速变化后产生的新产物不会覆盖
        旧产物，避免并发写入同一个文件互相截断。

        返回 (path, trim)：trim 为 (start_ms, end_ms)，
        表示播放时只播这一段（丢掉头尾无意义静音）；拿不到时间轴时为 None。
        """
        import asyncio
        import edge_tts
        path = None
        last = None
        for attempt in range(3):
            rv = self._rate_ver
            path = os.path.join(tmp, '%04d_%d.mp3' % (idx, rv))
            box = {}

            async def _fetch():
                # 用 stream() 而不是 save()：音频字节完全一样（实测同帧同字节），
                # 但能顺带拿到逐词时间轴，用来算头尾静音该裁多少。
                com = edge_tts.Communicate(
                    text, voice=self._voice, rate=self._rate_str(),
                    boundary='WordBoundary')
                buf = bytearray()
                first = None
                last_end = 0.0
                async for chunk in com.stream():
                    kind = chunk.get('type')
                    if kind == 'audio':
                        buf += chunk['data']
                    elif kind == 'WordBoundary':
                        off = chunk['offset'] / 1e7
                        dur = chunk.get('duration', 0) / 1e7
                        if first is None:
                            first = off
                        if off + dur > last_end:
                            last_end = off + dur
                if not buf:
                    raise RuntimeError('empty audio')
                with open(path, 'wb') as f:
                    f.write(bytes(buf))
                box['first'] = first
                box['last'] = last_end

            try:
                asyncio.run(asyncio.wait_for(_fetch(), timeout=25))
                if rv != self._rate_ver:
                    continue  # 合成途中倍速又变了，用新倍速重新合成
                return path, self._trim_of(box)
            except Exception as e:
                last = e
                time.sleep(0.8)
        if last is not None:
            raise last
        raise RuntimeError('synthesis failed')

    @staticmethod
    def _trim_of(box):
        """由逐词时间轴算出该裁掉的播放区间（毫秒）。"""
        first = box.get('first')
        last = box.get('last') or 0.0
        if first is None or last <= 0:
            return None
        start_ms = int(max(0.0, first * 1000.0 - TRIM_LEAD_MARGIN_MS))
        end_ms = int(last * 1000.0 + TRIM_TAIL_MARGIN_MS)
        if end_ms - start_ms < 200:
            return None          # 太短就不裁了，避免异常数据把内容削掉
        return (start_ms, end_ms)

    def set_volume(self, vol):
        """音量 0~100；标记变化，由播放线程在下一轮循环里即时生效。"""
        self._volume = int(max(0, min(100, vol)))
        self._volume_changed = True

    def set_fallback(self, fn):
        """网络中断时，把剩余内容交给本地语音继续读。"""
        self._fallback = fn

    # ---------- 线程 ----------
    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self):
        while True:
            item = self._cmdq.get()
            if item is None:
                break
            kind = item[0]
            if kind == 'speak':
                self._do_speak()
            elif kind == 'stop':
                self._hard_stop()

    def _hard_stop(self):
        try:
            self._player.stop()
            self._player.close()
        except Exception:
            pass

    # ---------- 核心 ----------
    def speak(self, chunks, on_progress=None, on_state=None, offset=0):
        """开始朗读。若正在朗读，会先停下旧任务再读新的。
        offset：从第 offset 段开始（0 表示从头），用于倍速重读。"""
        chunks = list(chunks)
        offset = max(0, min(offset, max(0, len(chunks) - 1)))
        with self._lock:
            self._pending = (chunks, on_progress, on_state, offset)
            self._active_chunks = chunks
            self._active_idx = offset
            self._active_pb = on_progress
            self._active_st = on_state
        self._stop_evt.set()   # 让正在读的旧任务尽快退出
        self._paused_evt.clear()
        self._ensure_thread()
        self._cmdq.put(('speak', None))

    def _do_speak(self):
        with self._lock:
            chunks, on_progress, on_state, offset = self._pending
        self._stop_evt.clear()
        self._paused_evt.clear()
        try:
            import asyncio
            import edge_tts
        except Exception:
            if on_state:
                on_state(self._T('unavailable'))
            return
        total = len(chunks)
        if total == 0 or offset >= total:
            if on_state:
                on_state(self._T('empty'))
            return
        if on_progress:
            on_progress(0, total)
        if on_state:
            on_state(self._T('preparing', offset + 1, total))
        tmp = tempfile.mkdtemp(prefix='neu_tts_')
        ready = queue.Queue(maxsize=max(4, SYNTH_PREFETCH * 2))
        stop = self._stop_evt
        handed_off = False
        # 全局并发闸门：整个朗读过程最多 SYNTH_PREFETCH 个合成请求同时在飞
        sem = threading.Semaphore(SYNTH_PREFETCH)
        # 记账：同一段绝不重复合成。
        # 重复合成会让两个线程同时写同一个 mp3，互相截断导致文件损坏、
        # 播放器打不开 → 表现为「进度在走但没有声音」。必须去重。
        book = {'requested': set(), 'inflight': 0, 'lock': threading.Lock()}

        def _put(item):
            while not stop.is_set():
                try:
                    ready.put(item, timeout=0.2)
                    return
                except queue.Full:
                    continue

        def synth_task(gen, i):
            """合成第 i 段。成功/失败都回报给主线程，由主线程按序播放。"""
            sem.acquire()
            try:
                if stop.is_set() or gen != self._synth_gen:
                    return
                path, trim = self._synth_one(chunks[i - 1], i, tmp)
                _put(('ok', i, path, self._rate_ver, gen, trim))
            except Exception:
                _put(('err', i, '', self._rate_ver, gen, None))
            finally:
                sem.release()

        def request(gen, indices):
            """请求合成若干段；已经在队列里的段不会重复请求。"""
            fresh = []
            with book['lock']:
                for i in indices:
                    if 1 <= i <= total and i not in book['requested']:
                        book['requested'].add(i)
                        fresh.append(i)
                book['inflight'] += len(fresh)
            for i in fresh:
                threading.Thread(target=synth_task, args=(gen, i),
                                 daemon=True).start()

        # 并发合成后各段返回顺序是乱的（谁先合成完谁先到）。
        # 用缓冲按段号重新排序：只有"下一段该播的"到手了才交给播放线程。
        pending = {}
        next_idx = offset + 1

        def take_ready(timeout=0.5):
            """按段号顺序取下一段；合成结果乱序到达时先缓存等待。

            返回 None 表示本轮超时（没有新的段就绪）。
            """
            nonlocal next_idx
            deadline = time.time() + timeout
            while True:
                item = pending.pop(next_idx, None)
                if item is not None:
                    next_idx += 1
                    return item
                # 收走已经到达的结果，顺序对的直接返回，顺序不对的先存起来
                while True:
                    try:
                        item = ready.get_nowait()
                    except queue.Empty:
                        break
                    with book['lock']:
                        book['inflight'] -= 1
                    if item[4] != self._synth_gen:
                        continue              # 旧代际产物，丢弃
                    if item[1] == next_idx:
                        next_idx += 1
                        return item
                    pending[item[1]] = item
                if time.time() >= deadline:
                    return None
                time.sleep(0.03)

        def drain():
            while True:
                try:
                    ready.get_nowait()
                except queue.Empty:
                    return

        with self._lock:
            self._synth_gen += 1
            gen = self._synth_gen
        # 首发一批：立即合成第 offset+1 段（等它出声），
        # 同时并发预取后面几段，保证首句尽快开读。
        request(gen, range(offset + 1,
                           min(total + 1, offset + 1 + SYNTH_PREFETCH)))
        try:
            while True:
                if self._stop_evt.is_set():
                    break
                item = take_ready(0.5)
                if item is None:
                    # 没有就绪的段：判断是"还在合成"还是"已经读完/无望"
                    if next_idx > total:
                        break
                    with book['lock']:
                        idle = (book['inflight'] <= 0 and not pending)
                    if idle:
                        break   # 合成线程都收了却还缺段，不死等
                    continue
                kind, i, path, ver, g, trim = item
                if g != self._synth_gen:
                    continue  # 旧一代的预合成产物，丢弃
                if kind == 'err':
                    fb = self._fallback
                    if fb is not None and i > 1:
                        handed_off = True
                        if on_state:
                            on_state(self._T('fallback'))
                        fb(chunks[i - 1:], on_progress, on_state)
                    else:
                        if on_state:
                            on_state(self._T('net_fail'))
                    break
                if ver != self._rate_ver:
                    # 朗读中倍速变了：丢掉旧倍速的产物，这一段按新倍速重合成，
                    # 后续段全部重新排队（requested 清空，避免残留旧倍速文件）
                    drain()
                    pending.clear()
                    with book['lock']:
                        book['requested'] = set()
                        book['inflight'] = 0
                    with self._lock:
                        self._synth_gen += 1
                        gen = self._synth_gen
                    try:
                        path, trim = self._synth_one(chunks[i - 1], i, tmp)
                    except Exception:
                        fb = self._fallback
                        if fb is not None and i > 1:
                            handed_off = True
                            if on_state:
                                on_state(self._T('fallback'))
                            fb(chunks[i - 1:], on_progress, on_state)
                        else:
                            if on_state:
                                on_state(self._T('net_fail'))
                        break
                    # 按新倍速重新并发预取后续段
                    request(gen, range(i + 1,
                                       min(total + 1, i + 1 + SYNTH_PREFETCH)))
                if self._paused_evt.is_set():
                    if on_state:
                        on_state(self._T('paused', max(1, i - 1), total))
                    while self._paused_evt.is_set():
                        if self._stop_evt.is_set():
                            break
                        self._paused_evt.wait(0.2)
                    if self._stop_evt.is_set():
                        break
                if self._stop_evt.is_set():
                    break
                if on_progress:
                    on_progress(i, total)
                if on_state:
                    on_state(self._T('reading', i, total))
                with self._lock:
                    self._active_idx = i
                self._play_file(path, trim)
                if i >= total:
                    break           # 最后一段已播完，收工
                # 滑动窗口补充：把预取窗口往后推一格。
                # request() 自带去重，重复调用也只会补进真正缺的那几段。
                with self._lock:
                    curgen = self._synth_gen
                if not stop.is_set():
                    request(curgen, range(i + 1,
                                          min(total + 1,
                                              i + 1 + SYNTH_PREFETCH)))
        finally:
            with self._lock:
                self._active_chunks = None
            shutil.rmtree(tmp, ignore_errors=True)
            if on_state and not self._stop_evt.is_set() and not handed_off:
                on_state(self._T('done'))
            self._paused_evt.clear()

    def _play_file(self, path, trim=None):
        """播放一段音频。trim=(start_ms, end_ms) 时只播这个区间，
        用来跳过 edge-tts 给每段加的头尾静音（否则段落之间会有约 0.7 秒空白）。"""
        try:
            import pythoncom
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass
        except Exception:
            pass
        use_trim = bool(trim and trim[1] > trim[0])

        def _start():
            """（重新）开始播放这一段，始终遵守裁剪区间。"""
            if use_trim:
                self._player.set_time_ms()
                self._player.play_range(trim[0], trim[1])
            else:
                self._player.play()

        try:
            self._player.open(path)
            self._player.set_playback_rate(self._play_rate)
            self._player.set_volume(self._volume)
            _start()
        except Exception:
            return  # 无声卡等环境直接跳过（本机正常出声）
        # 估算本段最长播放时间，防止卡死（裁过静音就按裁剪后的长度算）
        try:
            if use_trim:
                secs = (trim[1] - trim[0]) / 1000.0
            else:
                secs = os.path.getsize(path) * 8 / 48000.0
            dur = max(10.0, secs / max(0.5, self._play_rate) * 2 + 10)
        except Exception:
            secs = 0.0
            dur = 600.0
        # 记录本段的播放区间，供 play_progress() 换算播放进度
        if use_trim:
            self._cur_range = (trim[0], trim[1])
        elif secs > 0:
            self._cur_range = (0, int(secs * 1000))
        else:
            self._cur_range = None
        t0 = time.time()
        errs = 0            # 连续查询失败次数（用于区分偶发错误和真正结束）
        try:
            while True:
                if self._stop_evt.is_set():
                    break
                if self._volume_changed:
                    try:
                        self._player.set_volume(self._volume)
                    except Exception:
                        pass
                    self._volume_changed = False
                if time.time() - t0 > dur:
                    break
                if self._paused_evt.is_set():
                    try:
                        self._player.pause()
                    except Exception:
                        pass
                    while self._paused_evt.is_set():
                        if self._stop_evt.is_set():
                            break
                        self._paused_evt.wait(0.2)
                    if self._stop_evt.is_set():
                        break
                    try:
                        self._player.resume()
                    except Exception:
                        pass
                    if self._player.mode() == 'paused':
                        try:
                            _start()   # 兜底重开也要遵守裁剪区间
                        except Exception:
                            pass
                    continue
                # 高频检测"播完了没"：原来 0.2 秒轮询会在每段衔接处
                # 留下最多 0.2 秒的固定空档，倍速越高越明显（语音变快了，
                # 这个固定空档不跟着变快）。改成 0.03 秒，衔接几乎听不出来。
                mode = self._player.mode()
                if mode == 'stopped':
                    break
                if mode == '':
                    # 查询失败：可能是偶发错误，连续 3 次才认定已结束，
                    # 避免一次抖动就把整段掐断
                    errs += 1
                    if errs >= 3:
                        break
                else:
                    errs = 0
                time.sleep(0.03)
        finally:
            self._cur_range = None
            try:
                self._player.stop()
            except Exception:
                pass
            try:
                self._player.close()
            except Exception:
                pass

    # ---------- 控制 ----------
    def pause(self):
        self._paused_evt.set()

    def resume(self):
        self._paused_evt.clear()

    def stop(self):
        self._stop_evt.set()
        self._paused_evt.clear()

    def restart_for_speed(self):
        """倍速变化后平滑生效：不打断当前段落，下一段自动按新倍速合成。"""
        return

    def toggle_pause_resume(self):
        if self._paused_evt.is_set():
            self.resume()
            return 'resume'
        self.pause()
        return 'pause'

    def is_paused(self):
        return self._paused_evt.is_set()

    def play_progress(self):
        """当前这一段已经播了百分之多少（0~1）；拿不到返回 None。

        用于让正文跟着朗读进度滚动（长段落超出可视区时）。
        暂停时播放位置不动，所以这个值也不动，滚动会自然停住。
        """
        rng = getattr(self, '_cur_range', None)
        if not rng or rng[1] <= rng[0]:
            return None
        try:
            pos = self._player.position_ms()
        except Exception:
            pos = None
        if pos is None:
            return None
        a, b = rng
        try:
            return max(0.0, min(1.0, (float(pos) - a) / float(b - a)))
        except Exception:
            return None

    # ---------- 导出 ----------
    def export_mp3(self, text, out_path):
        import asyncio
        import edge_tts

        async def _do():
            await edge_tts.Communicate(
                text, voice=self._voice, rate=self._rate_str()).save(out_path)

        last = None
        for attempt in range(3):   # 导出失败自动重试，保证稳定输出
            try:
                asyncio.run(asyncio.wait_for(_do(), timeout=1200))
                return
            except Exception as e:
                last = e
                time.sleep(1.0)
        if last is not None:
            raise last


# ---------- 本地 SAPI 引擎（离线兜底） ----------

class SapiSpeaker:
    """Windows 原生 SAPI 引擎：逻辑沿用 v8（线程内 COM + 泵消息防死锁）。"""

    def __init__(self):
        self._lang = 'zh'
        self._voice = None
        self._rate = 0
        self._volume = 100
        self._stop_evt = threading.Event()
        self._paused_evt = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._cmdq = queue.Queue()
        self._gen = 0
        self._chunks = None
        self._active_idx = 0
        self._pb = None
        self._st = None

    def _get_voice(self):
        if self._voice is None:
            import comtypes.client
            import pythoncom
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass
            v = comtypes.client.CreateObject('SAPI.SpVoice')
            from comtypes.gen import SpeechLib  # noqa: F401 触发运行时生成绑定
            self._voice = v
            try:
                v.Volume = self._volume
            except Exception:
                pass
            self._select_zh_voice()
            return self._voice

    def _T(self, key, *args):
        msg = SPK_MSG.get(self._lang, SPK_MSG['zh']).get(key, key)
        return msg % args if args else msg

    def set_language(self, lang):
        self._lang = 'en' if lang == 'en' else 'zh'

    def _select_zh_voice(self):
        try:
            for tok in self._voice.GetVoices():
                try:
                    lang = str(tok.GetAttribute('Language')).lower()
                    desc = str(tok.GetDescription()).lower()
                except Exception:
                    lang, desc = '', ''
                if 'zh' in lang or 'chinese' in desc or '中文' in desc:
                    self._voice.Voice = tok
                    return
        except Exception:
            pass

    def list_voices(self):
        try:
            v = self._get_voice()
            out = []
            for tok in v.GetVoices():
                try:
                    name = str(tok.GetDescription())
                except Exception:
                    try:
                        name = str(tok.Id)
                    except Exception:
                        name = self._T('unknown_voice')
                out.append({'id': tok.Id, 'name': name})
            return out
        except Exception:
            return []

    def set_voice(self, voice_id):
        try:
            v = self._get_voice()
            for tok in v.GetVoices():
                try:
                    if tok.Id == voice_id:
                        v.Voice = tok
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def current_voice_id(self):
        try:
            v = self._get_voice()
            return str(v.Voice.Id)
        except Exception:
            return ''

    def set_rate(self, mult):
        import math
        try:
            self._rate = max(-10, min(20, int(round(10 * math.log2(mult)))))
        except Exception:
            self._rate = 0
        try:
            if self._voice is not None:
                self._voice.Rate = self._rate
        except Exception:
            pass

    def set_volume(self, vol):
        self._volume = int(max(0, min(100, vol)))
        try:
            if self._voice is not None:
                self._voice.Volume = self._volume
        except Exception:
            pass

    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self):
        while True:
            item = self._cmdq.get()
            if item is None:
                break
            kind = item[0]
            if kind == 'speak':
                self._do_speak(*item[1:])
            elif kind == 'stop':
                self._hard_stop()

    def _hard_stop(self):
        try:
            if self._voice is not None:
                self._voice.Speak('', 3)  # 3 = SVSFPurgeSpeakStream
        except Exception:
            pass

    def _speak_sentence(self, sent):
        v = self._get_voice()
        try:
            self._voice.Rate = self._rate
        except Exception:
            pass
        try:
            self._voice.Volume = self._volume
        except Exception:
            pass
        try:
            v.Speak(sent, 1)
        except Exception:
            v.Speak(sent, 0)
        while True:
            if self._stop_evt.is_set():
                try:
                    self._voice.Speak('', 3)
                except Exception:
                    pass
                break
            if self._paused_evt.is_set():
                try:
                    self._voice.Pause()
                except Exception:
                    pass
                while self._paused_evt.is_set():
                    if self._stop_evt.is_set():
                        break
                    time.sleep(0.2)
                if not self._stop_evt.is_set():
                    try:
                        self._voice.Resume()
                    except Exception:
                        pass
                continue
            pc = None
            try:
                import pythoncom
                pc = pythoncom
            except Exception:
                pass
            if pc is not None:
                try:
                    pc.PumpWaitingMessages()
                except Exception:
                    pass
            try:
                done = bool(self._voice.WaitUntilDone(200))
            except Exception:
                time.sleep(0.05)
                continue
            if done:
                break
            time.sleep(0.02)

    def speak(self, chunks, on_progress=None, on_state=None, offset=0):
        """开始朗读。offset：从第 offset 段开始（0 表示从头），用于倍速重读。"""
        chunks = list(chunks)
        offset = max(0, min(offset, max(0, len(chunks) - 1)))
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._chunks = chunks
            self._active_idx = offset
            self._pb = on_progress
            self._st = on_state
        self._stop_evt.set()   # 让旧任务在自己的线程里尽快退出（避免跨线程 COM 调用）
        self._paused_evt.clear()
        self._ensure_thread()
        self._cmdq.put(('speak', gen, chunks, on_progress, on_state, offset))

    def _do_speak(self, gen, chunks, on_progress, on_state, offset):
        self._stop_evt.clear()
        self._paused_evt.clear()
        total = len(chunks)
        if total == 0 or offset >= total:
            if on_state:
                on_state(self._T('empty'))
            return
        if on_progress:
            on_progress(0, total)
        for i, chunk in enumerate(chunks[offset:], offset + 1):
            if gen != self._gen or self._stop_evt.is_set():
                break
            if self._paused_evt.is_set():
                while self._paused_evt.is_set():
                    if gen != self._gen or self._stop_evt.is_set():
                        break
                    self._paused_evt.wait(0.2)
                if gen != self._gen or self._stop_evt.is_set():
                    break
            if on_progress:
                on_progress(i - 1, total)
            if on_state:
                on_state(self._T('reading_local', i, total))
            with self._lock:
                self._active_idx = i
            for sent in _split_sentences(chunk):
                if gen != self._gen or self._stop_evt.is_set():
                    break
                self._speak_sentence(sent)
            if gen != self._gen or self._stop_evt.is_set():
                break
            if on_progress:
                on_progress(i, total)
        if on_state and gen == self._gen and not self._stop_evt.is_set():
            on_state(self._T('done'))
        if gen == self._gen:
            with self._lock:
                self._chunks = None
                self._active_idx = 0
        self._paused_evt.clear()

    def restart_for_speed(self):
        """倍速变化后平滑生效：当前句子读完，下一句自动按新倍速朗读。"""
        return

    def pause(self):
        self._paused_evt.set()

    def resume(self):
        self._paused_evt.clear()

    def stop(self):
        self._stop_evt.set()
        self._paused_evt.clear()
        self._cmdq.put(('stop', ''))

    def toggle_pause_resume(self):
        if self._paused_evt.is_set():
            self.resume()
            return 'resume'
        self.pause()
        return 'pause'

    def is_paused(self):
        return self._paused_evt.is_set()

    def export_wav(self, text, out_path):
        v = self._get_voice()
        import comtypes.client
        from comtypes.gen import SpeechLib
        try:
            v.Rate = self._rate
        except Exception:
            pass
        try:
            v.Volume = self._volume
        except Exception:
            pass
        stream = comtypes.client.CreateObject('SAPI.SpFileStream')
        stream.Open(out_path, SpeechLib.SSFMCreateForWrite)
        old = v.AudioOutputStream
        v.AudioOutputStream = stream
        try:
            v.Speak(text, 0)
        finally:
            v.AudioOutputStream = old
            stream.Close()


# ---------- 统一门面 ----------

class Speaker:
    """统一入口。engine_mode: 'neural'（AI 网络语音）| 'sapi'（本地语音）。"""

    def __init__(self, engine_mode='neural'):
        self.engine_mode = engine_mode
        self._lang = 'zh'
        self._neural = NeuralSpeaker()
        self._sapi = SapiSpeaker()
        self._active = self._pick_active()

    def _T(self, key, *args):
        msg = SPK_MSG.get(self._lang, SPK_MSG['zh']).get(key, key)
        return msg % args if args else msg

    def set_language(self, lang):
        self._lang = 'en' if lang == 'en' else 'zh'
        self._neural.set_language(self._lang)
        self._sapi.set_language(self._lang)

    def _pick_active(self):
        if self.engine_mode == 'neural' and NeuralSpeaker.available():
            return 'neural'
        return 'sapi'

    def set_engine_mode(self, mode):
        self.engine_mode = mode
        self._active = self._pick_active()

    @property
    def active_engine(self):
        return self._active

    def list_voices(self):
        if self._active == 'neural':
            return self._neural.list_voices()
        return self._sapi.list_voices()

    def set_voice(self, voice_id):
        if self._active == 'neural':
            return self._neural.set_voice(voice_id)
        return self._sapi.set_voice(voice_id)

    def current_voice_id(self):
        if self._active == 'neural':
            return self._neural.current_voice_id()
        return self._sapi.current_voice_id()

    def speak(self, chunks, on_progress=None, on_state=None):
        self.stop()
        if self._active == 'neural':
            self._neural.speak(chunks, on_progress, on_state)
        else:
            self._sapi.speak(chunks, on_progress, on_state)

    def set_fallback(self, fn):
        """AI 网络中断时，把剩余内容交给本地语音继续读。"""
        self._neural.set_fallback(fn)

    def speak_local(self, chunks, on_progress=None, on_state=None):
        """用本地系统语音朗读（断网兜底，读完后 UI 控制也跟随本地引擎）。"""
        self._active = 'sapi'
        self._sapi.speak(chunks, on_progress, on_state)

    def restart_for_speed(self):
        """倍速变化后立即生效：AI 语音从当前段重读，本地语音同样立即生效。"""
        if self._active == 'neural':
            self._neural.restart_for_speed()
        else:
            self._sapi.restart_for_speed()

    def pause(self):
        if self._active == 'neural':
            self._neural.pause()
        else:
            self._sapi.pause()

    def resume(self):
        if self._active == 'neural':
            self._neural.resume()
        else:
            self._sapi.resume()

    def stop(self):
        self._neural.stop()
        self._sapi.stop()

    def toggle_pause_resume(self):
        if self._active == 'neural':
            return self._neural.toggle_pause_resume()
        return self._sapi.toggle_pause_resume()

    def is_paused(self):
        if self._active == 'neural':
            return self._neural.is_paused()
        return self._sapi.is_paused()

    def play_progress(self):
        """当前段播放进度 0~1；本地语音引擎或取不到时返回 None。"""
        if self._active == 'neural':
            try:
                return self._neural.play_progress()
            except Exception:
                return None
        return None

    def set_rate(self, mult):
        self._neural.set_rate(mult)
        self._sapi.set_rate(mult)

    def set_volume(self, vol):
        self._neural.set_volume(vol)
        self._sapi.set_volume(vol)

    def export(self, text, out_path):
        """导出音频：AI→MP3，本地→WAV。返回 (ok, 实际路径, 提示)。"""
        if self._active == 'neural':
            try:
                self._neural.export_mp3(text, out_path)
                return True, out_path, self._T('export_mp3')
            except Exception as e:
                return False, None, self._T('export_fail') % e
        try:
            wav = out_path
            if out_path.lower().endswith('.mp3'):
                wav = out_path[:-4] + '.wav'
            self._sapi.export_wav(text, wav)
            return True, wav, self._T('export_wav')
        except Exception as e:
            return False, None, self._T('export_wav_fail') % e
