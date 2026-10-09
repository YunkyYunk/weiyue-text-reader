# -*- coding: utf-8 -*-
"""文档内容提取：PDF / Word(.docx .doc) / 纯文本 / EPUB / MOBI / FB2，含朗读整理工具"""
import os
import re


_BOX_CHARS = set(chr(c) for c in range(0x2500, 0x2580))  # 表格框线等装饰字符

_PAGE_ONLY_RE = re.compile(r'^[-–—·\s]*\d{1,4}[-–—·\s]*$')       # 纯页码
_PAGE_LABEL_RE = re.compile(r'^第\s*\d{1,4}\s*[页章]\s*$')        # 第X页/第X章
_PAGE_DASH_RE = re.compile(r'^\s*[-–—]\s*\d{1,4}\s*[-–—]\s*$')   # - 12 -
_FOOTER_RE = re.compile(r'^(页码|第\s*\d+\s*页\s*共\s*\d+\s*页|'
                        r'Copyright|©|All\s+Rights\s+Reserved)',
                        re.I)


def _is_noise(line):
    """智能过滤：判断一行是否为页码/页眉页脚等朗读噪音。"""
    if not line:
        return False
    if _PAGE_ONLY_RE.match(line) and len(line) <= 12:
        return True
    if _PAGE_LABEL_RE.match(line):
        return True
    if _PAGE_DASH_RE.match(line):
        return True
    if _FOOTER_RE.match(line) and len(line) <= 30:
        return True
    return False


_LINE_BREAKS = '\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029'


def clean_for_speech_with_map(text, smart_filter=True):
    """整理成适合朗读的文本，并返回「清洗后 → 原文」的字符位置映射。

    返回 (cleaned, mapping)：
      cleaned      整理后的文本；
      mapping[i]   cleaned 里第 i 个字符在**原始文本**中的下标；
                   换行等无对应字符的位置为 -1。

    为什么要这个映射：朗读分段是基于 cleaned 的，而界面显示的是原文。
    没有映射就只能拿分段结果去原文里"找"，一旦清洗改动过文字（去缩进、
    去页码、合并标点）就会找不到，导致那一段朗读时不亮。有映射后可精确对应。
    """
    src = text or ''
    rows = []                    # 每个元素：list[(字符, 原文下标)] 或 None(空行)
    raw_pos = 0
    for piece in src.splitlines(keepends=True):
        base = raw_pos
        raw_pos += len(piece)
        body = piece.rstrip(_LINE_BREAKS)
        kept = [(ch, base + k) for k, ch in enumerate(body)
                if ch not in _BOX_CHARS]
        core = ''.join(ch for ch, _ in kept)
        stripped = core.strip()
        if not stripped:
            rows.append(None)
            continue
        if smart_filter and _is_noise(stripped):
            continue             # 噪音行整行丢弃
        l = len(core) - len(core.lstrip())
        r = len(core.rstrip())
        rows.append(kept[l:r])

    # 与原逻辑一致：连续空行只保留一个，且去掉结尾空行
    final = []
    blank = False
    for row in rows:
        if row is None:
            if not blank:
                final.append(None)
            blank = True
        else:
            blank = False
            final.append(row)
    while final and final[-1] is None:
        final.pop()

    parts, mapping = [], []
    last = len(final) - 1
    for k, row in enumerate(final):
        if row is None:
            parts.append('\n')
            mapping.append(-1)
        else:
            parts.extend(ch for ch, _ in row)
            mapping.extend(i for _, i in row)
            if k != last:
                parts.append('\n')
                mapping.append(-1)
    return ''.join(parts), mapping


def clean_for_speech(text, smart_filter=True):
    """整理成适合朗读的文本：去掉表格框线/装饰字符、压缩连续空行；
    smart_filter=True 时跳过页码、页眉页脚等噪音行。"""
    return clean_for_speech_with_map(text, smart_filter)[0]


# 句末标点：句子真正结束的地方
_SENT_END = '。！？…'
# 可用于切段的标点：除了句末标点，逗号/顿号/分号/冒号也算。
# 这样「取距 40 字边界最近的标点」才能把片段填到接近 40 字，
# 而不是一遇到顿号就提前断开。切在逗号后是自然停顿，朗读听感正常。
_CUT_PUNCT = '。！？…，、；：'
# 无实际含义的连续标点，统一并成一个再送朗读（省略号有含义，保留）
_REPEAT_PUNCT = re.compile(r'([!?！？,，、;；])(?:[!?！？,，、;；])+')
# 会被合并的标点字符
_REPEAT_CHARS = '!?！？,，、;；'


def _normalize_punct(text):
    """把无意义的连续标点并成一个（！！ → ！、，， → ，）。
    省略号……保留原样，它有实际含义。"""
    if not text:
        return text
    # 省略号占位保护，避免被通用规则影响
    protect = '\x00DOT\x00'
    text = text.replace('……', protect).replace('…', protect)
    text = _REPEAT_PUNCT.sub(r'\1', text)
    text = text.replace(protect, '……')
    return text


def _normalize_with_map(text, mapping):
    """与 _normalize_punct 等价，但同步调整字符位置映射。

    单独实现是因为归一化会改变长度：单个 '…' 会被还原成 '……'（+1 字），
    连续标点会被合并（-N 字）。这些偏移必须同步到 mapping，
    否则分段位置与原文的对应关系会随时间累积错位。
    """
    if not text:
        return text, list(mapping or [])
    src = list(mapping) if mapping else [-1] * len(text)
    out, out_map = [], []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if text.startswith('……', i):
            out.append('……')
            out_map.append(src[i])
            out_map.append(src[i + 1])
            i += 2
            continue
        if ch == '…':
            # 单个 '…' 还原成 '……'，两个字符都指回原位置
            out.append('……')
            out_map.append(src[i])
            out_map.append(src[i])
            i += 1
            continue
        if ch in _REPEAT_CHARS:
            out.append(ch)
            out_map.append(src[i])
            j = i + 1
            while j < n and text[j] in _REPEAT_CHARS:
                j += 1
            i = j
            continue
        out.append(ch)
        out_map.append(src[i])
        i += 1
    return ''.join(out), out_map


def split_chunks(text, max_len=40, hard_limit=80):
    """按标点/换行切块：以 max_len 字为基准，取距基准边界最近的标点断开。

    规则（实测 488 万字长篇调优）：
    1. **换行优先**：若 max_len 字（40）窗口内出现换行，就切在**第一个**
       换行之后。作者用换行分行的短句（如「痛！/好痛！/头好痛！」）本就
       是各自独立的段落，必须各自成段，否则会并成一个多行大块，
       朗读时正文无法逐行跟随；
    2. 窗口内没有换行时，取**最靠后**的标点（。！？…，、；：）之后断开 ——
       即"取距 40 字边界最近的标点"，让每段尽量填到接近 40 字；
    3. 窗口内既无换行也无标点，允许延到 hard_limit 字（80）再按同样顺序
       找一个；仍然没有才按 hard_limit 硬切（长篇里几乎不会发生）。
    段长不追求整齐：靠近边界的短段（如拟声词、独立成行的短句）照原样
    保留，总字数不变就不会产生额外开销。
    """
    text = _normalize_punct(text or '')
    out, i, n = [], 0, len(text)
    if n == 0:
        return out

    def find_cut(start, limit):
        """在 [start, start+limit) 内定切点，返回切点之后的绝对下标。

        换行优先（取第一个换行），否则取最靠后的标点；找不到返回 -1。
        """
        win = text[start:start + limit]
        nl = win.find('\n')
        if nl >= 0:
            return start + nl + 1
        cut = -1
        for k, ch in enumerate(win):
            if ch in _CUT_PUNCT:
                cut = k
        return start + cut + 1 if cut >= 0 else -1

    while i < n:
        end = find_cut(i, max_len)
        if end < 0:
            end = find_cut(i, hard_limit)
        if end < 0:
            end = i + hard_limit
        if end <= i:            # 防御：保证永远前进
            end = i + 1
        if end > n:
            end = n
        seg = text[i:end]
        if out and not seg.strip():
            # 切出来的只是空白（章节之间的空行）：并进上一段，
            # 不让它单独成段，否则会白发一次合成请求、还读不出东西。
            # 注意必须"并入"而不是"丢弃"——分段要保持是清洗后文本的
            # 连续切片，否则字符位置映射会错位。
            out[-1] += seg
        else:
            out.append(seg)
        i = end
    return out


def _map_chunks_to_raw(chunks, mapping):
    """把分段映射回原始文本的字符区间。返回 [(start, end) 或 None]。

    分段是"清洗后文本"的连续切片，mapping[k] 记录了清洗后第 k 个字符在
    原始文本里的下标，所以逐段直接查表即可，不需要去原文里搜索字符串。
    """
    n = len(mapping)

    def first_raw(k):
        while k < n and mapping[k] < 0:
            k += 1
        return mapping[k] if k < n else None

    def last_raw(k):
        while k >= 0 and mapping[k] < 0:
            k -= 1
        return mapping[k] if k >= 0 else None

    ranges = []
    off = 0
    for ch in chunks:
        s, e = off, off + len(ch)
        off = e
        if e <= s:
            ranges.append(None)
            continue
        a = first_raw(s)
        b = last_raw(min(e - 1, n - 1))
        if a is None or b is None or b < a:
            ranges.append(None)
        else:
            ranges.append((a, b + 1))
    return ranges


def prepare_for_reading(text, smart_filter=True):
    """朗读前的一步到位处理：清洗 → 标点归一 → 分段 → 位置映射。

    返回 (chunks, ranges)：
      chunks     朗读分段（送 TTS 的文本）
      ranges[i]  第 i 段在**原始文本**中的 (start, end)；定位不到时为 None

    用它的原因：分段基于清洗后的文本，而界面显示的是原文。若拿分段结果
    去原文里"找位置"，一旦清洗改过文字（去缩进/去页码/合并标点）就找不到，
    会导致那段朗读时不亮。这里用位置映射精确对应，不再依赖搜索。
    """
    cleaned, cmap = clean_for_speech_with_map(text, smart_filter)
    norm, nmap = _normalize_with_map(cleaned, cmap)
    chunks = split_chunks(norm)      # 内部再归一一次，但归一是幂等的，位置不变
    return chunks, _map_chunks_to_raw(chunks, nmap)


def extract_text(path):
    """按扩展名提取文本。读不了时抛异常(由上层转成友好提示)。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.pdf':
        return extract_pdf(path)
    elif ext == '.docx':
        return extract_docx(path)
    elif ext == '.doc':
        return extract_doc(path)
    elif ext in ('.epub',):
        return extract_epub(path)
    elif ext in ('.mobi', '.azw', '.azw3'):
        return extract_mobi(path)
    elif ext in ('.fb2',):
        return extract_fb2(path)
    elif ext in ('.html', '.htm'):
        return extract_html(path)
    elif ext in ('.txt', '.md', '.text'):
        return read_text_file(path)
    else:
        raise ValueError(f"暂不支持此格式 ({ext or '无扩展名'})")


def read_text_file(path):
    """读纯文本：自动识别常见中文编码(UTF-8 / GBK / UTF-16)，避免乱码。"""
    with open(path, 'rb') as f:
        raw = f.read()
    # 1) 尝试 UTF-8
    try:
        return raw.decode('utf-8')
    except (UnicodeDecodeError, ValueError):
        pass
    # 2) 尝试 GBK（中文 Windows ANSI 最常见）
    try:
        return raw.decode('gbk')
    except (UnicodeDecodeError, ValueError):
        pass
    # 3) 尝试 UTF-16 (带 BOM)
    for enc in ('utf-16', 'gb18030', 'big5'):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    # 4) 最后兜底：忽略错误
    return raw.decode('utf-8', errors='ignore')


def extract_pdf(path):
    import pymupdf  # PyMuPDF
    doc = pymupdf.open(path)
    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()
    return '\n'.join(pages)


def extract_docx(path):
    """Word .docx（现代 XML 格式）用 python-docx 提取。"""
    import docx
    d = docx.Document(path)
    parts = []
    # 正文段落
    for para in d.paragraphs:
        if para.text.strip():
            parts.append(para.text)
    # 表格
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(' | '.join(cells))
    return '\n'.join(parts)


def extract_epub(path):
    """EPUB 电子书：本质是 zip，把里面的 XHTML/HTML 正文提取出来。"""
    import zipfile
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    texts = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith(('.xhtml', '.html', '.htm'))]
        names.sort()
        for n in names:
            try:
                p = _Text()
                p.feed(z.read(n).decode('utf-8', errors='ignore'))
                chunk = ''.join(p.parts).strip()
                if chunk:
                    texts.append(chunk)
            except Exception:
                continue
    return '\n'.join(texts)


def _mobi_unpack_palmdoc(data):
    """MOBI 用的 PalmDoc 压缩解压（LZ77 变体），返回解压后的字节。"""
    out = bytearray()
    p = 0
    n = len(data)
    while p < n:
        c = data[p]
        p += 1
        if 1 <= c <= 8:
            # 短字面量：直接复制 c 个字节
            out.extend(data[p:p + c])
            p += c
        elif c < 128:
            # 单个字面字节
            out.append(c)
        elif c >= 192:
            # 空格 + 一个字节（c ^ 128）
            out.append(0x20)
            out.append(c ^ 128)
        else:
            # 128..191：回指复制
            if p >= n:
                break
            c = (c << 8) | data[p]
            p += 1
            dist = (c >> 3) & 0x7FF
            length = (c & 7) + 3
            if dist > length:
                out.extend(out[-dist:length - dist])
            else:
                for _ in range(length):
                    out.append(out[-dist])
    return bytes(out)


def _mobi_strip_html(html_text):
    """把 MOBI 的 HTML 正文转成纯文本（解析实体，保留段落换行）。"""
    from html.parser import HTMLParser
    import html as _html

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

        def handle_entityref(self, name):
            self.parts.append('&%s;' % name)

        def handle_charref(self, name):
            self.parts.append('&#%s;' % name)

    p = _Text()
    p.feed(html_text)
    return _html.unescape(''.join(p.parts))


def extract_mobi(path):
    """MOBI / AZW 电子书：解析 PDB 结构 + PalmDoc 解压，取出正文。

    MOBI 本质是 Palm 数据库（PDB）：第 0 条记录是 PalmDoc 头 + MOBI 头，
    之后的正文记录用 PalmDoc 压缩算法存 HTML。这里只做纯文本提取，
    不解析目录、封面等元数据。
    """
    import struct

    with open(path, 'rb') as f:
        raw = f.read()
    if len(raw) < 78:
        raise ValueError('文件太小，不是有效的 MOBI 电子书')
    nrec = struct.unpack('>H', raw[76:78])[0]
    if nrec < 2:
        raise ValueError('MOBI 电子书里没有正文记录')
    offsets = []
    for i in range(nrec):
        offsets.append(struct.unpack('>I', raw[78 + i * 8: 78 + i * 8 + 4])[0])

    rec0 = raw[offsets[0]: offsets[1] if nrec > 1 else len(raw)]
    compression = struct.unpack('>H', rec0[0:2])[0]
    text_len = struct.unpack('>I', rec0[4:8])[0]

    # MOBI 头在第 0 条记录的 16 字节之后；第一个图片记录号在 0x5C 处
    first_image = None
    if len(rec0) >= 16 + 0x60 and rec0[16:20] == b'MOBI':
        first_image = struct.unpack('>I', rec0[16 + 0x5C: 16 + 0x5C + 4])[0]

    end = nrec
    if first_image and 1 < first_image < nrec:
        end = first_image

    parts = []
    for i in range(1, end):
        s = offsets[i]
        e = offsets[i + 1] if i + 1 < nrec else len(raw)
        seg = raw[s:e]
        if compression == 2:
            seg = _mobi_unpack_palmdoc(seg)
        parts.append(seg)
    data = b''.join(parts)
    if text_len and 0 < text_len < len(data):
        data = data[:text_len]

    text = None
    for enc in ('utf-8', 'gb18030'):
        try:
            text = data.decode(enc)
            break
        except (UnicodeDecodeError, ValueError):
            continue
    if text is None:
        text = data.decode('utf-8', errors='ignore')
    return _mobi_strip_html(text)


def extract_fb2(path):
    """FB2 电子书（XML）：把正文段落按顺序拼出来。"""
    import xml.etree.ElementTree as ET

    raw = read_text_file(path)
    root = ET.fromstring(raw)

    def local(tag):
        return tag.rsplit('}', 1)[-1]

    leaf_tags = ('p', 'subtitle', 'text-author', 'v')

    parts = []
    for el in root.iter():
        t = local(el.tag)
        if t not in leaf_tags:
            continue
        # 只取最底层的段落，避免父节点(如 title/poem)重复收集
        has_leaf_child = False
        for child in el.iter():
            if child is el:
                continue
            if local(child.tag) in leaf_tags:
                has_leaf_child = True
                break
        if not has_leaf_child:
            txt = ''.join(el.itertext()).strip()
            if txt:
                parts.append(txt)
    if not parts:
        raise ValueError('FB2 电子书里没有提取到正文')
    return '\n'.join(parts)


def extract_html(path):
    """网页/HTML 文件：去掉标签只留文字。"""
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    p = _Text()
    p.feed(read_text_file(path))
    return ''.join(p.parts)


def extract_doc(path):
    """Word .doc（旧版二进制格式）读取。

    优先用本机安装的 Microsoft Word 通过 COM 打开并取文本（最可靠，
    需装了 Microsoft Office/Word）。若本机没装 Word，返回 None，
    由上层引导把文件另存为 .docx。
    """
    try:
        # 尝试用 Word 的 COM 接口打开（仅 Windows + 装了 Office 时有效）
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        try:
            doc = word.Documents.Open(os.path.abspath(path), ReadOnly=True)
            try:
                text = doc.Content.Text
            finally:
                doc.Close(False)
            return text
        finally:
            word.Quit()
    except Exception:
        pass
    return None


def extract_chunks(text, words_per_chunk=120):
    """按句切成长度不超 words_per_chunk 的分块，用于逐句朗读。"""
    import re
    sentences = re.split(r'(?<=[。！？；…\n.])\s*', text)
    chunks, cur, cnt = [], [], 0
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        cur.append(s)
        cnt += len(s)
        if cnt >= words_per_chunk:
            chunks.append(''.join(cur))
            cur, cnt = [], 0
    if cur:
        chunks.append(''.join(cur))
    return chunks
