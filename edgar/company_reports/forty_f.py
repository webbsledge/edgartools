"""Form 40-F annual report for Canadian MJDS filers."""
import re
from functools import cached_property
from typing import List, Optional, Tuple

from rich.console import Group, Text
from rich.panel import Panel

from edgar.company_reports._base import CompanyReport
from edgar.richtools import repr_rich

__all__ = ['FortyF']


# ---------------------------------------------------------------------------
# AIF identification
# ---------------------------------------------------------------------------

_SKIP_TYPES = frozenset({
    'GRAPHIC', 'EX-101.SCH', 'EX-101.CAL', 'EX-101.DEF',
    'EX-101.LAB', 'EX-101.PRE', 'XML', '',
    'EX-23.1', 'EX-23.2', 'EX-23.3', 'EX-23.4',
    'EX-31.1', 'EX-31.2', 'EX-32.1', 'EX-32.2',
    'EX-97', 'EX-97.1', 'EX-32',
})


def _scan_attachments(attachments):
    """Classify 40-F attachments by AIF-likelihood tier."""
    ex1_candidates = []
    aif_desc_candidates = []
    ex991_named_candidates = []
    ex991_unnamed_candidates = []
    main_40f = None

    for att in attachments:
        doc_type = (getattr(att, 'document_type', '') or '').strip()
        desc = (getattr(att, 'description', '') or '').upper()
        url = str(getattr(att, 'url', '') or '')
        filename = url.split('/')[-1].lower()

        if doc_type in _SKIP_TYPES:
            continue
        if not url.endswith(('.htm', '.html', '.xhtml')):
            continue

        if doc_type in ('EX-1', 'EX-1.1', 'EX-1.2'):
            ex1_candidates.append(att)
        elif 'ANNUAL INFORMATION' in desc or re.search(r'\bAIF\b', desc):
            aif_desc_candidates.append(att)
        elif doc_type == 'EX-99.1' and any(
            kw in filename for kw in ('annual', 'aif', 'annualinformation')
        ):
            ex991_named_candidates.append(att)
        elif doc_type == 'EX-99.1':
            ex991_unnamed_candidates.append(att)
        elif doc_type in ('40-F', '40-F/A'):
            main_40f = att

    return ex1_candidates, aif_desc_candidates, ex991_named_candidates, ex991_unnamed_candidates, main_40f


def _find_aif_attachment(filing) -> Tuple[Optional[object], str]:
    """Find the Annual Information Form (AIF) attachment in a 40-F filing.

    Priority chain:
    1. EX-1 / EX-1.1 / EX-1.2 (standard MJDS AIF exhibits)
    2. Description containing 'ANNUAL INFORMATION' or 'AIF'
    3. EX-99.1 with AIF keywords in filename
    4. EX-99.1 unnamed — use size ratio to distinguish AIF from supplementary data
    5. Main 40-F document (CNQ inline pattern)

    Uses ``filing.attachments`` (SGML-parsed, no extra network hop) for
    priorities 1-3.  Falls back to ``filing.homepage.attachments`` only when
    we reach priority 4 and need file sizes for the size-ratio heuristic.
    """
    (ex1, aif_desc, ex991_named,
     ex991_unnamed, main_40f) = _scan_attachments(filing.attachments)

    # Priorities 1-3 don't need sizes
    if ex1:
        return ex1[0], 'EX-1/EX-1.1/EX-1.2 (standard MJDS)'
    if aif_desc:
        return aif_desc[0], 'Description mentions ANNUAL INFORMATION'
    if ex991_named:
        return ex991_named[0], 'EX-99.1 with AIF filename keywords'

    # Priority 4: unnamed EX-99.1 — need sizes to distinguish Barrick (EX-99.1
    # IS the AIF) from CNQ (EX-99.1 is supplementary data, main doc is AIF).
    # Rescan via homepage.attachments which always carry file sizes.
    if ex991_unnamed:
        (_, _, _, hp_ex991_unnamed,
         hp_main_40f) = _scan_attachments(filing.homepage.attachments)

        ex991_att = hp_ex991_unnamed[0] if hp_ex991_unnamed else ex991_unnamed[0]
        ex991_size = getattr(ex991_att, 'size', None) or 0
        hp_main_size = (getattr(hp_main_40f, 'size', None) or 0) if hp_main_40f else 0

        # If sizes are unknown, default to EX-99.1 (more likely to be AIF)
        if ex991_size == 0 and hp_main_size == 0:
            return ex991_att, 'EX-99.1 first major exhibit (likely AIF)'

        # CNQ: EX-99.1 (624 KB) << 40-F (6.3 MB), ratio ~0.10 → main doc is AIF
        # Barrick: EX-99.1 (1.79 MB) >> 40-F (505 KB), ratio > 1 → EX-99.1 is AIF
        if hp_main_size > 0 and ex991_size / hp_main_size < 0.20:
            return hp_main_40f or main_40f, '40-F main document (AIF embedded inline)'
        return ex991_att, 'EX-99.1 first major exhibit (likely AIF)'

    # Priority 5: main 40-F document (no separate AIF exhibit at all)
    if main_40f:
        return main_40f, '40-F main document (AIF embedded inline)'

    return None, 'AIF not found'


# ---------------------------------------------------------------------------
# Business-section extraction (regex on plain text)
# ---------------------------------------------------------------------------

_BUSINESS_STARTS = [
    r'(?:NARRATIVE\s+)?DESCRIPTION\s+OF\s+THE\s+BUSINESS',
    r'BUSINESS\s+OF\s+THE\s+(?:MANAGER|COMPANY|CORPORATION|TRUST|FUND)',
    r'DESCRIPTION\s+OF\s+BUSINESS',
    r'BUSINESS\s+OVERVIEW',
    r'DESCRIPTION\s+OF\s+OPERATIONS',
]

_BUSINESS_ENDS = [
    r'GENERAL\s+DEVELOPMENT\s+OF\s+THE\s+BUSINESS',
    r'(?:THREE[\s-]YEAR|3[\s-]YEAR)\s+HISTORY',
    r'DESCRIPTION\s+OF\s+CAPITAL\s+STRUCTURE',
    r'MATERIAL\s+PROPERTIES',
    r'LEGAL\s+(?:PROCEEDINGS|MATTERS)',
    r'RISK\s+FACTORS',
    r'CODE\s+OF\s+BUSINESS\s+CONDUCT',
    r'MARKET\s+FOR\s+SECURITIES',
    r'DIRECTORS\s+AND\s+(?:EXECUTIVE\s+OFFICERS|OFFICERS|EXECUTIVE)',
]

# Section patterns for items detection (NI 51-102 AIF headings)
_SECTION_PATTERNS = [
    r'CORPORATE\s+STRUCTURE',
    r'GENERAL\s+DEVELOPMENT\s+OF\s+THE\s+BUSINESS',
    r'(?:NARRATIVE\s+)?DESCRIPTION\s+OF\s+THE\s+BUSINESS',
    r'BUSINESS\s+OF\s+THE\s+(?:MANAGER|COMPANY|CORPORATION|TRUST|FUND)',
    r'DESCRIPTION\s+OF\s+CAPITAL\s+STRUCTURE',
    r'MARKET\s+FOR\s+SECURITIES',
    r'DIVIDENDS(?:\s+AND\s+DISTRIBUTIONS)?',
    r'DIRECTORS\s+AND\s+(?:EXECUTIVE\s+OFFICERS|OFFICERS|EXECUTIVE)',
    r'RISK\s+FACTORS',
    r'LEGAL\s+(?:PROCEEDINGS|MATTERS)',
    r'MATERIAL\s+PROPERTIES',
    r'CODE\s+OF\s+BUSINESS\s+CONDUCT',
]


def _is_toc_entry(text: str, match) -> bool:
    """Detect TOC entries: inline page numbers or multi-line page numbers."""
    after = text[match.end():match.end() + 100]
    # Inline TOC: digit immediately follows header (CNQ style)
    stripped = after.lstrip()
    if re.match(r'^\d+\w*', stripped):
        return True
    # Multi-line TOC: page number on a subsequent line (RY style)
    lines = after.split('\n')
    for line in lines[1:4]:
        s = line.strip().strip('\xa0')
        if s and re.match(r'^\d+[\-\d]*\*?\s*$', s):
            return True
    return False


def _is_cross_reference(text: str, match) -> bool:
    """Detect cross-references: quoted mentions or mid-sentence inline references."""
    before = text[max(0, match.start() - 80):match.start()]
    # In quotes
    if re.search(r'["\u201c\u201d]\s*$', before):
        return True
    # 'See X', 'under X'
    if re.search(r'\b(?:see|under)\s+["\u201c]?\s*$', before, re.IGNORECASE):
        return True
    # Mid-sentence: lowercase word ending on the SAME LINE immediately before
    # match — but exclude page footers (gap ends with a Capitalized word like
    # "Annual Information Form" or "Annual Report").
    last_newline = before.rfind('\n')
    gap = before[last_newline + 1:] if last_newline >= 0 else before
    if gap.strip() and re.search(r'[a-z][,;:]\s*$', gap):
        # Punctuation like comma/semicolon/colon → clearly mid-sentence
        return True
    if gap.strip() and re.search(r'[a-z]\s+$', gap):
        # Ends with a lowercase word followed by space — mid-sentence
        # But not if the last word is capitalized (page footer pattern)
        last_word = gap.strip().split()[-1] if gap.strip() else ''
        if last_word and last_word[0].islower():
            return True
    return False


def _find_first_clean_match(text: str, pattern: str, min_pos: int):
    """Find the first match past *min_pos* that is not a TOC entry or cross-reference."""
    for m in re.finditer(pattern, text, re.IGNORECASE):
        if m.start() > min_pos and not _is_toc_entry(text, m) and not _is_cross_reference(text, m):
            return m
    return None


def _find_section_positions(full_text: str) -> List[Tuple[int, str]]:
    """Detect all NI 51-102 section headers and their positions in AIF text."""
    min_content_pos = max(5000, int(len(full_text) * 0.03))
    found = []
    for pattern in _SECTION_PATTERNS:
        m = _find_first_clean_match(full_text, pattern, min_content_pos)
        if m:
            name = re.sub(r'\s+', ' ', m.group()).strip()
            found.append((m.start(), name))
    found.sort(key=lambda t: t[0])
    return found


def _extract_section_text(full_text: str, positions: List[Tuple[int, str]], idx: int) -> str:
    """Extract text for section at *idx*, ending where the next section begins."""
    start = positions[idx][0]
    end = positions[idx + 1][0] if idx + 1 < len(positions) else len(full_text)
    return re.sub(r'\s+', ' ', full_text[start:end]).strip()


def _extract_business_section(full_text: str) -> Optional[str]:
    """Extract the business description section from AIF plain text.

    Uses the shared section-detection pipeline, then extracts text for
    the business section bounded by the next detected section.
    """
    positions = _find_section_positions(full_text)
    if not positions:
        return None

    # Find the business section among detected positions
    for idx, (_, name) in enumerate(positions):
        upper = name.upper()
        if ('DESCRIPTION' in upper and 'BUSINESS' in upper) or \
           ('BUSINESS OF THE' in upper):
            return _extract_section_text(full_text, positions, idx)

    # Fallback: use start/end pattern matching (original algorithm)
    min_content_pos = max(5000, int(len(full_text) * 0.03))
    for pattern in _BUSINESS_STARTS:
        m = _find_first_clean_match(full_text, pattern, min_content_pos)
        if m:
            start_pos = m.start()
            end_pos = len(full_text)
            search_from = start_pos + 500
            for end_pattern in _BUSINESS_ENDS:
                end_m = _find_first_clean_match(full_text, end_pattern, search_from)
                if end_m:
                    end_pos = min(end_pos, end_m.start())
            return re.sub(r'\s+', ' ', full_text[start_pos:end_pos]).strip()

    return None


# ---------------------------------------------------------------------------
# FortyF class
# ---------------------------------------------------------------------------


class FortyF(CompanyReport):
    """Canadian MJDS annual report (Form 40-F).

    The 40-F wraps the Canadian Annual Information Form (AIF), which is the
    primary source of business description text.  The 40-F wrapper document
    itself typically contains iXBRL financial statement data.

    Usage::

        filing = Company("SHOP").get_filings(form="40-F").latest()
        forty_f = filing.obj()          # FortyF instance
        forty_f.business                # business description text from AIF
        forty_f.financials              # XBRL financials (from base class)
    """

    def __init__(self, filing):
        assert filing.form in ('40-F', '40-F/A'), (
            f"Expected 40-F or 40-F/A, got {filing.form}"
        )
        super().__init__(filing)

    # -- AIF discovery -------------------------------------------------------

    @cached_property
    def _aif_result(self) -> Tuple[Optional[object], str]:
        return _find_aif_attachment(self._filing)

    @cached_property
    def aif_attachment(self):
        """The AIF exhibit attachment, or ``None`` if not found."""
        return self._aif_result[0]

    @cached_property
    def _aif_html(self) -> Optional[str]:
        """Raw HTML of the AIF document (single download, shared by consumers)."""
        att = self.aif_attachment
        if att is None:
            return None
        from edgar.httprequests import download_text
        return download_text(att.url)

    @cached_property
    def aif_document(self):
        """Parsed ``Document`` from the AIF exhibit HTML."""
        html = self._aif_html
        if not html:
            return None
        from edgar.documents import HTMLParser, ParserConfig
        parser = HTMLParser(ParserConfig(form='40-F'))
        return parser.parse(html)

    @cached_property
    def document(self):
        """Override base class: returns AIF document so ``.items`` / ``[]`` work.

        If a separate AIF exhibit exists its parsed ``Document`` is returned.
        Otherwise falls back to the main 40-F filing HTML (CNQ inline pattern).
        """
        aif_doc = self.aif_document
        if aif_doc is not None:
            return aif_doc
        # Fallback to base-class behaviour (parses filing.html())
        html = self._filing.html()
        if not html:
            return None
        from edgar.documents import HTMLParser
        from edgar.documents.config import ParserConfig
        parser = HTMLParser(ParserConfig(form='40-F'))
        return parser.parse(html)

    # -- Business section ----------------------------------------------------

    @cached_property
    def _aif_text(self) -> Optional[str]:
        """Plain text of the AIF document (cached for reuse)."""
        html = self._aif_html
        if not html:
            return None
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')
            return soup.get_text()
        except ImportError:
            # bs4 unavailable — strip tags with a rough regex
            text = re.sub(r'<[^>]+>', ' ', html)
            return re.sub(r'\s+', ' ', text)

    @cached_property
    def business(self) -> Optional[str]:
        """Business description from the AIF 'Description of the Business' section."""
        text = self._aif_text
        if text is None:
            return None
        return _extract_business_section(text)

    # -- Section listing / lookup --------------------------------------------

    @cached_property
    def _section_positions(self) -> List[Tuple[int, str]]:
        """Detected sections as (start_position, Title Case name) pairs."""
        text = self._aif_text
        if text is None:
            return []
        positions = _find_section_positions(text)
        return [(pos, name.title()) for pos, name in positions]

    @cached_property
    def items(self) -> List[str]:
        """Detected AIF sections (Canadian NI 51-102 headings, not US Item numbers)."""
        return [name for _, name in self._section_positions]

    def __getitem__(self, key: str) -> Optional[str]:
        """Look up a section by name.

        Accepts the exact title from ``.items`` (e.g. ``"Risk Factors"``)
        or a case-insensitive match.  Returns the section text between
        this header and the next detected header.

        Examples::

            forty_f["Risk Factors"]
            forty_f["Description Of The Business"]
            forty_f["dividends"]       # case-insensitive
        """
        text = self._aif_text
        positions = self._section_positions
        if not text or not positions:
            return None

        key_lower = key.lower().strip()
        for idx, (_, name) in enumerate(positions):
            if name.lower() == key_lower:
                return _extract_section_text(text, positions, idx)

        return None

    # -- Display -------------------------------------------------------------

    def __str__(self):
        return f"FortyF('{self.company}')"

    def __rich__(self):
        aif_reason = self._aif_result[1]
        aif_info = Text(f"AIF: {aif_reason}", style="dim")
        return Panel(
            Group(
                self._filing.__rich__(),
                aif_info,
                self.financials or Text("No financial data available"),
            )
        )

    def __repr__(self):
        return repr_rich(self.__rich__())
