"""
등기부등본(부동산 등기사항전부증명서) 분석 핵심 로직 — 멀티 문서(건물+토지) 지원 버전.

구조 (3단):
    1. parse_deed_document(raw_text) -> DeedDocument
       등기부 '한 부'를 파싱해 사실만 담는다 (판정 없음).
       표제부에서 문서 종류(집합건물/일반건물/토지)를 자동 분류한다.
    2. analyze_property(documents, ...) -> AnalysisResult
       문서 여러 부를 병합해 물건 단위로 판정한다.
       - 토지·건물 공동담보 근저당 중복 합산 방지 (접수번호 기반 dedup)
       - 건물·토지 소유자 교차 검증
       - 필수 문서 누락 감지 → analysis_status = "NEEDS_MORE_DOCS"
    3. analyze_deed_text(raw_text, ...) -> AnalysisResult
       기존 단일 문서 진입점 (하위 호환 래퍼). 집합건물 1부 경로는 동작 동일.

부동산 유형(property_type):
    집합건물(등기부 1부로 충분): APARTMENT, ROW_HOUSE(연립), MULTI_FAMILY(다세대), OFFICETEL
    비집합건물(건물+토지 각 1부 필요): SINGLE_FAMILY(단독), MULTI_HOUSEHOLD(다가구)

analysis_status (백엔드가 분기 처리하는 기계 판독용 상태):
    COMPLETE        정상 분석 완료
    NEEDS_MORE_DOCS 추가 등기부 필요 (required_documents에 종류, reason에 사유)
                    → 백엔드는 사용자에게 추가 업로드를 요청하고,
                      기존 PDF + 새 PDF를 sources에 모두 담아 재분석 메시지를 발행한다.

보증기관 기준 출처 (2026년 7월 확인):
    HUG 전세보증금반환보증:
      - 비아파트 주택가격 = 공시가격 × 140%, 담보인정비율 90% → '126% 룰'
        (담보인정비율 90%→80% 하향은 현 시점 시행 검토되지 않음 — HUG 공식 입장)
      - 선순위채권 ≤ 주택가격 60%, 주택가격 ≤ 12억, 권리침해 없을 것
      - 단독·다가구는 '선순위 임차보증금'까지 부채에 포함해 계산해야 함
    LH 전세임대 권리분석:
      - 부채비율(총부채/주택가격) ≤ 90% (단독·다가구 등 유형에 따라 80% 적용례)
      - 선순위 설정최고액 ≤ 주택가격 50%
      - 압류·가압류·가처분·가등기·경매신청 등 소유권 행사 제한 시 불가
        (전세권·지상권 설정 물건도 권리분석 통과가 어려움)

주의: 이 로직은 '스크리닝(1차 자동 점검)'용이다. 최종 권리분석은 전문가 확인이 필요하다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 위험 키워드 정의
# ---------------------------------------------------------------------------

# 권리제한/분쟁 소지 키워드 (갑구에서 주로 발견)
# LH 기준에 따라 '가등기' 계열 추가 (소유권이전청구권가등기 등 — 소유권 행사 제한사항)
ENCUMBRANCE_KEYWORDS = [
    "가압류",
    "가처분",
    "압류",
    "경매개시결정",
    "임의경매",
    "강제경매",
    "예고등기",
    "환매특약",
    "소유권이전청구권가등기",
    "가등기",
]

# 신탁 관련 키워드
TRUST_KEYWORDS = ["신탁", "신탁등기", "담보신탁", "관리신탁", "처분신탁"]

# 근저당권 관련 (을구)
MORTGAGE_KEYWORDS = ["근저당권설정", "근저당권"]

# 을구의 용익물권 계열 — LH 권리분석에서 기피/불가 사유 (전세권·지상권·지역권)
# 토지 등기부에서 특히 의미가 크다 (지상권은 토지 등기부 을구에 설정됨)
LAND_RIGHT_KEYWORDS = ["전세권설정", "전세권", "지상권설정", "지상권", "지역권설정", "지역권"]
HOUSING_LEASE_RIGHT_KEYWORDS = ["주택임차권"]

# 말소 표시
CANCELLED_MARKERS = ["말소", "해지", "해제"]

# ---------------------------------------------------------------------------
# 보증기관 기준 상수 (정책 변경 시 이 상수들만 업데이트)
# ---------------------------------------------------------------------------

PUBLIC_PRICE_MULTIPLIER = 1.40       # 공시가격 → 주택가격 환산 배수 (HUG 비아파트 산정 방식)

# HUG 전세보증금반환보증
HUG_LTV_RATIO = 0.90                 # 담보인정비율 (140% × 90% = '126% 룰')
HUG_SENIOR_LIEN_LIMIT = 0.60         # 선순위채권 ≤ 주택가격의 60%
HUG_MAX_HOUSE_PRICE = 1_200_000_000  # 주택가격 상한 12억

# LH 전세임대
LH_SENIOR_LIEN_LIMIT = 0.50          # 선순위 설정최고액 ≤ 주택가격의 50%
LH_DEBT_RATIO_LIMIT_DEFAULT = 0.90   # 부채비율 한도 (기본)
LH_DEBT_RATIO_LIMIT_BY_TYPE = {
    # 단독·다가구는 유형에 따라 80%가 적용되는 사례가 있어 보수적으로 80% 적용
    "SINGLE_FAMILY": 0.80,
    "MULTI_HOUSEHOLD": 0.80,
}

# 월세 보증금 회수 시뮬레이션용 보수적 처분가 비율.
# 실제 경매 낙찰가·배당액 또는 법정 기준이 아니라, 서비스의 1차 스크리닝 가정이다.
MONTHLY_RENT_RECOVERY_VALUE_RATIO = 0.70

# ---------------------------------------------------------------------------
# 부동산 유형 / 문서 종류
# ---------------------------------------------------------------------------

# 문서 종류 (표제부에서 자동 분류)
DOC_COLLECTIVE = "COLLECTIVE"  # 집합건물 등기부 (전유부분 + 대지권)
DOC_BUILDING = "BUILDING"      # 일반건물 등기부
DOC_LAND = "LAND"              # 토지 등기부
DOC_UNKNOWN = "UNKNOWN"

COLLECTIVE_PROPERTY_TYPES = {"APARTMENT", "ROW_HOUSE", "MULTI_FAMILY", "OFFICETEL", "COLLECTIVE"}
NON_COLLECTIVE_PROPERTY_TYPES = {"SINGLE_FAMILY", "MULTI_HOUSEHOLD"}

# 등기부의 큰 섹션 제목
SECTION_HEADER_RE = re.compile(r"【\s*([가-힣\s]+?)\s*】")

# 표제부 기반 문서 분류 패턴 (공백 유연 매칭)
_COLLECTIVE_MARKERS_RE = re.compile(
    r"1\s*동의\s*건물의\s*표시|전유부분의\s*건물의\s*표시|대지권의\s*표시"
)
_DAEJIGWON_RE = re.compile(r"대지권의\s*표시")
_BUILDING_MARKER_RE = re.compile(r"건물의\s*표시")
_LAND_MARKER_RE = re.compile(r"토지의\s*표시")
LAND_PARCEL_RE = re.compile(r"([가-힣]+(?:동|읍|면|리))\s*\n?\s*(\d+(?:-\d+)?)")
JOINT_LAND_PARCEL_RE = re.compile(
    r"토지\s+(?:[^\n]*?\s)?([가-힣]+(?:동|읍|면|리))\s*\n?\s*(\d+(?:-\d+)?)"
)

# 집합건물 표제부의 '토지 별도등기 있음' 이력
SEPARATE_LAND_REGISTRY_RE = re.compile(r"토지\s*별도\s*등기\s*있음|별도\s*등기\s*있음")
SEPARATE_LAND_REGISTRY_CANCELLED_RE = re.compile(r"\d+\s*번\s*별도\s*등기\s*말소")

PROPERTY_TYPE_KEYWORDS = [
    ("MULTI_HOUSEHOLD", ["다가구주택", "다가구"]),
    ("MULTI_FAMILY", ["다세대주택", "다세대"]),
    ("ROW_HOUSE", ["연립주택", "연립"]),
    ("OFFICETEL", ["오피스텔"]),
    ("APARTMENT", ["아파트"]),
    ("SINGLE_FAMILY", ["단독주택", "단독"]),
]

# 등기부 표제부에서 물건 식별에 충분한 주소 핵심값만 추출한다.
# 지번 주소는 "전농동 152-73", 도로명 주소는 "서울시립대로 112-1"처럼 비교한다.
JIBUN_ADDRESS_RE = re.compile(r"([가-힣]+(?:동|읍|면|리)\s*\d+(?:\s*-\s*\d+)?)")
ROAD_ADDRESS_RE = re.compile(r"([가-힣0-9]+(?:로|길)\s*\d+(?:\s*-\s*\d+)?)")


def classify_deed_type(pyojebu_text: str) -> str:
    """
    표제부 텍스트로 등기부 종류를 분류한다.

    - 집합건물: "1동의 건물의 표시" / "전유부분의 건물의 표시" / "대지권의 표시"
    - 토지:    "(토지의 표시)"  ※ 집합건물의 "(대지권의 목적인 토지의 표시)"와
               겹치므로 반드시 집합건물 판별을 먼저 한다.
    - 일반건물: "(건물의 표시)" ※ "1동의 건물의 표시"와 겹치므로 역시 집합건물 우선.
    """
    if not pyojebu_text or not pyojebu_text.strip():
        return DOC_UNKNOWN
    if _COLLECTIVE_MARKERS_RE.search(pyojebu_text):
        return DOC_COLLECTIVE
    if _BUILDING_MARKER_RE.search(pyojebu_text):
        return DOC_BUILDING
    if _LAND_MARKER_RE.search(pyojebu_text):
        return DOC_LAND
    return DOC_UNKNOWN


def has_active_separate_land_registry(pyojebu_text: str) -> bool:
    """
    표제부의 대지권 표시에서 현재 유효한 '별도등기 있음'만 판별한다.

    말소사항 포함 등기부에는 과거 별도등기와 이후의 'n번 별도등기 말소'가 함께
    표시된다. 마지막 유효 기록이 말소이면 토지 등기부를 추가로 요구하지 않는다.
    '중 일부말소' 뒤에 다시 표시된 '별도등기 있음'은 남은 권리가 있다는 뜻이므로
    마지막 별도등기 기록이 우선한다.
    """
    if not pyojebu_text:
        return False

    active_matches = list(SEPARATE_LAND_REGISTRY_RE.finditer(pyojebu_text))
    if not active_matches:
        return False

    cancelled_matches = list(SEPARATE_LAND_REGISTRY_CANCELLED_RE.finditer(pyojebu_text))
    if not cancelled_matches:
        return True

    return active_matches[-1].start() > cancelled_matches[-1].start()


def infer_property_type_from_pyojebu(pyojebu_text: str, doc_type: str) -> Optional[str]:
    """
    표제부의 건물내역/용도 문구로 주택 유형을 추정한다.

    등기부 표제부에는 보통 "공동주택(다세대주택)", "다가구주택",
    "오피스텔" 같은 용도 문자열이 들어간다. 이 값은 사용자가 propertyType을
    보내지 않았을 때 보증/위험도 판정의 기본 유형으로 사용한다.
    """
    if not pyojebu_text or doc_type == DOC_LAND:
        return None

    compact = re.sub(r"\s+", "", pyojebu_text)
    for property_type, keywords in PROPERTY_TYPE_KEYWORDS:
        if any(keyword in compact for keyword in keywords):
            return property_type

    if doc_type == DOC_COLLECTIVE:
        return "COLLECTIVE"
    if doc_type == DOC_BUILDING:
        return "SINGLE_FAMILY"
    return None


def extract_land_parcels(text: str, *, joint_collateral_only: bool = False) -> list[str]:
    """텍스트에서 '전농동 152-73' 형태의 토지 필지 식별자를 추출한다."""
    pattern = JOINT_LAND_PARCEL_RE if joint_collateral_only else LAND_PARCEL_RE
    parcels: list[str] = []
    for dong, lot_number in pattern.findall(text):
        parcel = f"{dong} {lot_number}"
        if parcel not in parcels:
            parcels.append(parcel)
    return parcels


def extract_registry_addresses(pyojebu_text: str) -> list[str]:
    """표제부에서 지번·도로명 주소의 비교용 핵심값을 중복 없이 추출한다."""
    addresses: list[str] = []
    for pattern in (JIBUN_ADDRESS_RE, ROAD_ADDRESS_RE):
        for match in pattern.finditer(pyojebu_text or ""):
            address = re.sub(r"\s+", " ", match.group(1)).strip()
            if address not in addresses:
                addresses.append(address)
    return addresses


def _address_identifiers(address: Optional[str]) -> set[str]:
    """주소 전체 표현의 차이를 줄인 지번·도로명 핵심 식별자 집합을 만든다."""
    if not address:
        return set()

    normalized = unicodedata.normalize("NFKC", address)
    identifiers: set[str] = set()
    for pattern in (JIBUN_ADDRESS_RE, ROAD_ADDRESS_RE):
        for match in pattern.finditer(normalized):
            identifiers.add(re.sub(r"[\s-]+", "", match.group(1)))
    return identifiers


def _find_matching_registry_address(
    expected_address: Optional[str], registry_addresses: list[str]
) -> Optional[str]:
    """입력 주소의 지번 또는 도로명 핵심값과 일치하는 등기부 주소를 찾는다."""
    expected_identifiers = _address_identifiers(expected_address)
    if not expected_identifiers:
        return None

    for registry_address in registry_addresses:
        if expected_identifiers & _address_identifiers(registry_address):
            return registry_address
    return None


def split_sections(text: str) -> dict[str, str]:
    """
    등기부 텍스트를 【 표제부 】 / 【 갑구 】 / 【 을구 】 / 【 매매목록 】 등의
    섹션 제목 기준으로 잘라 각 섹션의 본문만 반환한다.
    같은 이름의 섹션이 여러 번 나오면(예: 표제부가 2번) 이어붙인다.
    """
    matches = list(SECTION_HEADER_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        name = re.sub(r"\s+", "", m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        sections[name] = sections.get(name, "") + "\n" + body
    return sections


# ---------------------------------------------------------------------------
# 결과 구조
# ---------------------------------------------------------------------------

@dataclass
class DeedDocument:
    """등기부 '한 부'의 파싱 결과 — 사실만 담고, 판정은 하지 않는다."""
    doc_type: str = DOC_UNKNOWN
    inferred_property_type: Optional[str] = None
    current_owner: Optional[str] = None
    owner_names: list[str] = field(default_factory=list)
    encumbrance_hits: list[dict[str, Any]] = field(default_factory=list)
    trust_hits: list[dict[str, Any]] = field(default_factory=list)
    land_right_hits: list[dict[str, Any]] = field(default_factory=list)  # 을구 전세권/지상권 등
    tenant_right_hits: list[dict[str, Any]] = field(default_factory=list)  # 을구 주택임차권
    mortgage_items: list[dict[str, Any]] = field(default_factory=list)
    registry_addresses: list[str] = field(default_factory=list)
    land_parcels: list[str] = field(default_factory=list)
    joint_collateral_land_parcels: list[str] = field(default_factory=list)
    has_separate_land_registry: bool = False  # 집합건물 표제부 '토지 별도등기 있음'
    has_daejigwon: bool = False               # 집합건물 대지권 등기 여부
    bulk_sale_warning: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """AnalysisResult.documents에 넣을 부별 요약 (payload 크기 절제)."""
        return {
            "doc_type": self.doc_type,
            "inferred_property_type": self.inferred_property_type,
            "current_owner": self.current_owner,
            "active_mortgage_count": sum(
                1 for m in self.mortgage_items if m["status"] == "active"
            ),
            "active_encumbrance_count": sum(
                1 for h in self.encumbrance_hits if not h["cancelled"]
            ),
            "active_tenant_right_count": sum(
                1 for h in self.tenant_right_hits if not h["cancelled"]
            ),
            "has_separate_land_registry": self.has_separate_land_registry,
            "has_daejigwon": self.has_daejigwon,
            "registry_addresses": self.registry_addresses,
            "land_parcels": self.land_parcels,
            "joint_collateral_land_parcels": self.joint_collateral_land_parcels,
            "notes": self.notes,
        }


@dataclass
class AnalysisResult:
    # ---- 분석 상태 (백엔드가 분기 처리하는 기계 판독용 필드) ----
    analysis_status: str = "COMPLETE"  # COMPLETE / NEEDS_MORE_DOCS
    required_documents: list[str] = field(default_factory=list)  # 예: ["LAND"]
    required_documents_reason: str = ""

    # ---- 물건/문서 정보 ----
    property_type: Optional[str] = None
    lease_type: str = "JEONSE"  # JEONSE / WOLSE
    documents: list[dict[str, Any]] = field(default_factory=list)  # 부별 요약
    registry_address: Optional[str] = None
    address_matches_submission: Optional[bool] = None
    address_match_basis: Optional[str] = None  # SUBMITTED_ADDRESS / ROAD_ADDRESS / MISMATCH / NOT_VERIFIABLE

    # ---- 소유자 ----
    current_owner: Optional[str] = None  # 건물(또는 집합건물) 기준 현재 소유자
    owner_names: list[str] = field(default_factory=list)
    owner_matches_contract: Optional[bool] = None
    building_land_owner_match: Optional[bool] = None  # 비집합건물: 건물·토지 소유자 일치 여부

    # ---- 권리관계 ----
    encumbrance_hits: list[dict[str, Any]] = field(default_factory=list)
    trust_found: bool = False
    trust_hits: list[dict[str, Any]] = field(default_factory=list)
    land_right_hits: list[dict[str, Any]] = field(default_factory=list)
    tenant_right_hits: list[dict[str, Any]] = field(default_factory=list)
    mortgage_total: int = 0  # dedup 후 활성 채권최고액 합계 (원)
    mortgage_items: list[dict[str, Any]] = field(default_factory=list)
    jeonse_rate: Optional[float] = None  # 보증금 ÷ 주택가격 × 100 (퍼센트)
    recovery_price_used: Optional[int] = None  # 월세 보증금 회수 추정에 사용한 보수적 처분가
    estimated_recoverable_deposit: Optional[int] = None  # 월세 보증금 예상 회수액
    deposit_recovery_rate: Optional[float] = None  # 월세 보증금 예상 회수율(퍼센트)
    senior_tenant_deposits_used: Optional[int] = None  # 다가구 선순위 임차보증금 (입력 시)
    registered_tenant_deposit_total: int = 0  # 등기부상 유효 주택임차권 보증금 합계

    # ---- 위험도 ----
    risk_ratio: Optional[float] = None
    risk_score: int = 0
    risk_level: str = "UNKNOWN"  # SAFE / CAUTION / WARNING / DANGER / UNKNOWN

    # ---- 보증기관 사전점검 ----
    hug_eligible: Optional[bool] = None
    hug_reasons: list[str] = field(default_factory=list)
    lh_eligible: Optional[bool] = None
    lh_reasons: list[str] = field(default_factory=list)
    house_price_used: Optional[int] = None
    house_price_basis: str = ""

    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 텍스트 정규화
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """전각/반각 통일, 중복 공백 정리."""
    text = unicodedata.normalize("NFKC", text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 개별 추출 함수 (기존 로직 유지)
# ---------------------------------------------------------------------------

ROW_START_RE = re.compile(r"^(\d+)(?:-(\d+))?\s+(\S.*)$")
OWNER_ROW_MARKER = "[[OWNER_ROW]]"
OWNER_ROW_RE = re.compile(r"^\[\[OWNER_ROW\]\](\d+(?:-\d+)?)\|([^|]*)\|([^|]*)$")

SHARE_MENTION_RE = re.compile(r"지분\s*(\d+)\s*분의\s*(\d+)")
NAME_RRN_RE = re.compile(r"([가-힣]{2,4})\s+\d{6}-")


def _extract_share_owners(block: str) -> list[tuple[str, str, str]]:
    """
    블록 안에서 "지분 N분의 M"과 그에 대응하는 이름을 짝짓는다.
    "지분 언급 이후, 다음 지분 언급 전까지 등장하는 첫 번째 이름"을 소유자로 판단.
    """
    share_positions = [(m.start(), m.end(), m.group(1), m.group(2)) for m in SHARE_MENTION_RE.finditer(block)]
    name_positions = [(m.start(), m.group(1)) for m in NAME_RRN_RE.finditer(block)]
    results: list[tuple[str, str, str]] = []
    for i, (_, s_end, denom, numer) in enumerate(share_positions):
        next_share_start = share_positions[i + 1][0] if i + 1 < len(share_positions) else len(block)
        candidate = next((name for (pos, name) in name_positions if s_end <= pos < next_share_start), None)
        if candidate:
            results.append((denom, numer, candidate))
    return results


SOLE_OWNER_RE = re.compile(r"(?:소유자|공유자)\s*[:：]?\s*([가-힣]{2,4})\s+\d{6}")
RENAME_RE = re.compile(r"([가-힣]{2,4})의\s*성명\s*\(명칭\)\s*([가-힣]{2,4})")
LOSING_SHARE_RE = re.compile(r"\d+번\s*([가-힣]{2,4})\s*지분")


def _structured_owner_rows(text: str) -> list[tuple[str, str, str]]:
    """PDF 표 셀에서 보존한 갑구 순위번호/등기목적/권리자 행을 읽는다."""
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        match = OWNER_ROW_RE.match(line.strip())
        if match:
            rows.append(match.groups())
    return rows


def _extract_owners_from_structured_rows(
    rows: list[tuple[str, str, str]]
) -> tuple[Optional[str], list[str]]:
    """갑구 표 셀을 기반으로 지분 원장을 갱신해 현재 소유자를 계산한다."""
    ledger: dict[str, Fraction] = {}
    history: list[str] = []

    def record(name: str) -> None:
        if name not in history:
            history.append(name)

    def new_owners_from_rights(rights: str) -> list[tuple[str, Fraction]]:
        shares = [
            (name, Fraction(int(numer), int(denom)))
            for denom, numer, name in _extract_share_owners(rights)
        ]
        if shares:
            return shares

        names = NAME_RRN_RE.findall(rights)
        if len(names) == 1:
            return [(names[0], Fraction(1, 1))]
        return []

    for _, purpose, rights in rows:
        is_bojon = "소유권보존" in purpose
        # 표의 등기목적 셀은 "1번OO지분전부 이전"처럼 '소유권' 단어가 생략되기도 한다.
        # 반면 근저당권이전은 권리자 셀에 근저당권자만 있으므로 소유자/공유자 표기로 구분한다.
        is_transfer = (
            "이전" in purpose
            and "가등기" not in purpose
            and ("소유자" in rights or "공유자" in rights)
        )
        if not is_bojon and not is_transfer:
            continue

        new_owners = new_owners_from_rights(rights)
        if not new_owners:
            continue

        source_names = LOSING_SHARE_RE.findall(purpose)
        if is_bojon:
            ledger.clear()
        elif source_names:
            for source_name in source_names:
                record(source_name)
                partial = re.search(
                    rf"\d+번\s*{re.escape(source_name)}\s*지분\s*\d+\s*분의\s*\d+"
                    rf"\s*중\s*일부\s*\(\s*(\d+)\s*분의\s*(\d+)\s*\)",
                    purpose,
                )
                if partial:
                    moved = Fraction(int(partial.group(2)), int(partial.group(1)))
                    ledger[source_name] = max(
                        Fraction(0, 1), ledger.get(source_name, Fraction(0, 1)) - moved
                    )
                else:
                    ledger[source_name] = Fraction(0, 1)
        else:
            # 참조 순위가 없는 소유권이전은 일반적으로 소유권 전부 이전이다.
            ledger.clear()

        for name, share in new_owners:
            ledger[name] = ledger.get(name, Fraction(0, 1)) + share
            record(name)

    current_owners = [name for name, share in ledger.items() if share > 0]
    return ", ".join(current_owners) if current_owners else None, history


def extract_owners(
    gapgu_text: str,
    structured_rows: Optional[list[tuple[str, str, str]]] = None,
) -> tuple[Optional[str], list[str]]:
    """갑구를 순서대로 읽으며 '현재 소유자(들)'를 지분 원장 방식으로 추적한다. (기존 로직)"""
    # 표 추출은 지분 일부 이전처럼 셀이 여러 줄로 갈라진 경우에 특히 신뢰할 수 있다.
    # 반면 일부 PDF는 다음 페이지의 표 선을 완전히 인식하지 못하므로, 일반 이전만 있는
    # 문서는 워터마크를 제거한 기존 텍스트 파서를 계속 사용한다.
    if structured_rows and any("중 일부" in purpose for _, purpose, _ in structured_rows):
        current_owner, history = _extract_owners_from_structured_rows(structured_rows)
        if current_owner:
            return current_owner, history

    blocks = _split_into_row_blocks(gapgu_text)

    ledger: dict[str, bool] = {}
    history: list[str] = []

    def _record(name: str) -> None:
        if name not in history:
            history.append(name)

    for _, block in blocks:
        if "개명" in block:
            for old, new in RENAME_RE.findall(block):
                was_owner = ledger.pop(old, None)
                if was_owner is not None:
                    ledger[new] = was_owner
                _record(new)

        header = "\n".join(block.splitlines()[:4])

        is_bojon = "소유권보존" in header
        is_transfer = (not is_bojon) and ("이전" in header) and ("가등기" not in header)
        if not is_bojon and not is_transfer:
            continue

        triples = _extract_share_owners(block)
        sole = SOLE_OWNER_RE.search(block)
        new_owners = [name for _, _, name in triples] or ([sole.group(1)] if sole else [])
        if not new_owners:
            continue

        if is_bojon:
            ledger.clear()
            for name in new_owners:
                ledger[name] = True
                _record(name)
            continue

        is_partial_share = "지분" in header
        if is_partial_share:
            for name in LOSING_SHARE_RE.findall(block):
                ledger[name] = False
            for name in new_owners:
                ledger[name] = True
                _record(name)
        else:
            ledger.clear()
            for name in new_owners:
                ledger[name] = True
                _record(name)

    current_owners = [n for n in history if ledger.get(n)]
    current_owner = ", ".join(current_owners) if current_owners else None
    return current_owner, history


def _to_won(num_text: str) -> int:
    digits = re.sub(r"[^\d]", "", num_text)
    return int(digits) if digits else 0


def _split_into_row_blocks(section_text: str) -> list[tuple[int, str]]:
    """섹션 텍스트를 순위번호(등기부 표의 각 행) 단위 블록으로 나눈다. (기존 로직)"""
    lines = section_text.splitlines()
    starts: list[tuple[int, int]] = []
    for idx, line in enumerate(lines):
        m = ROW_START_RE.match(line.strip())
        if m:
            starts.append((idx, int(m.group(1))))

    blocks: list[tuple[int, str]] = []
    for i, (start_idx, rank) in enumerate(starts):
        end_idx = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        block_text = "\n".join(lines[start_idx:end_idx])
        blocks.append((rank, block_text))
    return blocks


# 접수 정보: "2023년5월2일 (접수)? 제12345호" — 토지·건물 공동담보 동일성 판별의 핵심 키
RECEIPT_RE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:접수\s*)?제?\s*(\d+)\s*호")
# 근저당권자: "근저당권자 주식회사OO은행 ..." — 접수번호 파싱 실패 시 보조 키
CREDITOR_RE = re.compile(r"근저당권자\s*[:：]?\s*([가-힣A-Za-z0-9]+)")


def _extract_receipt_no(block: str) -> Optional[str]:
    m = RECEIPT_RE.search(block)
    if not m:
        return None
    y, mo, d, no = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}:{no}"


def _cancelled_ranks(blocks: list[tuple[int, str]]) -> set[int]:
    """실제 말소/해제 등기 행이 참조하는 순위번호만 말소로 처리한다."""
    cancelled: set[int] = set()
    for _, block in blocks:
        lines = block.splitlines()
        first_line = lines[0].strip() if lines else ""
        row_match = ROW_START_RE.match(first_line)
        if not row_match:
            continue

        purpose = row_match.group(3)
        if not re.match(r"^\d+\s*번", purpose):
            continue

        # 등기소 PDF의 말소 표시는 첫 행 또는 다음 행에 내려갈 수 있다. 다만
        # 페이지 하단의 "말소사항을 표시" 안내문은 등기 내용이 아니므로 제외한다.
        body_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("--") or stripped.startswith("*"):
                break
            body_lines.append(stripped)
        if not re.search(r"(?:말소|해제)", " ".join(body_lines)):
            continue

        # 말소 대상이 여러 건이면 목적란이 다음 줄로 이어질 수 있다.
        # 예: "1번근저당권설정," 다음 줄 "2번근저당권설정 ... 등기말소"
        # 첫 줄의 purpose만 보면 2번을 놓치므로, 현재 등기 행 전체에서 참조 순위를 찾는다.
        for ref_m in re.finditer(r"(\d+)(?:-\d+)?\s*번", " ".join(body_lines)):
            cancelled.add(int(ref_m.group(1)))
    return cancelled


def extract_mortgages(eulgu_text: str) -> tuple[int, list[dict[str, Any]]]:
    """
    을구에서 근저당권의 '채권최고액'을 순위번호 기준으로 추출·합산한다. (기존 로직 +
    접수번호/근저당권자 추출 추가 — 멀티 문서 병합 시 공동담보 dedup 키로 사용)
    """
    blocks = _split_into_row_blocks(eulgu_text)

    cancelled_ranks = _cancelled_ranks(blocks)
    mortgages: dict[int, dict[str, Any]] = {}

    for rank, block in blocks:
        first_line = block.splitlines()[0].strip() if block.splitlines() else ""
        amt_m = re.search(r"채권최고액\s*[:：]?\s*금?\s*([\d,]+)\s*원", block)
        is_setting = "근저당권설정" in first_line
        is_change = "근저당권변경" in first_line

        if is_setting and amt_m:
            creditor_m = CREDITOR_RE.search(block)
            mortgages[rank] = {
                "rank": rank,
                "raw": first_line,
                "amount": _to_won(amt_m.group(1)),
                "status": "active",
                "joint_collateral": "공동담보" in block,
                "receipt_no": _extract_receipt_no(block),
                "creditor": creditor_m.group(1) if creditor_m else None,
            }
            continue

        # 부기등기는 같은 순위의 근저당을 갱신한다. 특히 채권최고액 변경은
        # 최초 설정액이 아니라 가장 마지막 변경액을 사용해야 한다.
        mortgage = mortgages.get(rank)
        if mortgage is None:
            continue
        if is_change and amt_m:
            mortgage["amount"] = _to_won(amt_m.group(1))
            mortgage["raw"] = first_line
        if "공동담보" in block:
            mortgage["joint_collateral"] = True

    items = []
    total = 0
    for rank, mortgage in mortgages.items():
        mortgage["status"] = "cancelled" if rank in cancelled_ranks else "active"
        if mortgage["status"] == "active":
            items.append(mortgage)
            total += mortgage["amount"]

    items.sort(key=lambda x: x["rank"])
    return total, items


def extract_registry_items(section_text: str, keywords: list[str]) -> list[dict[str, Any]]:
    """섹션에서 키워드로 시작하는 설정 행을 찾고, 'N번...말소' 참조로 취소 여부 판정. (기존 로직)"""
    blocks = _split_into_row_blocks(section_text)

    cancelled_ranks = _cancelled_ranks(blocks)

    sorted_kws = sorted(keywords, key=len, reverse=True)
    items: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for rank, block in blocks:
        first_line = block.splitlines()[0].strip()
        m = ROW_START_RE.match(first_line)
        if not m:
            continue
        rest = m.group(3)
        if re.match(r"^\d+\s*번", rest):
            continue
        for kw in sorted_kws:
            if rest.startswith(kw):
                key = (rank, kw)
                if key in seen:
                    break
                seen.add(key)
                item = {
                    "keyword": kw,
                    "rank": rank,
                    "line": first_line,
                    "cancelled": rank in cancelled_ranks,
                }
                amount_m = re.search(r"임차보증금\s*[:：]?\s*금?\s*([\d,]+)\s*원", block)
                if amount_m:
                    item["amount"] = _to_won(amount_m.group(1))
                items.append(item)
                break

    items.sort(key=lambda x: x["rank"])
    return items


def detect_bulk_sale_warning(maemae_text: str) -> Optional[str]:
    """【매매목록】의 묶음 거래가액을 이 호수 가격으로 오인하지 않도록 경고. (기존 로직)"""
    if not maemae_text.strip():
        return None
    amt_m = re.search(r"거래가액\s*[:：]?\s*금?\s*([\d,]+)\s*원", maemae_text)
    if not amt_m:
        return None
    unit_count = len(re.findall(r"\[(?:건물|토지)\]", maemae_text))
    amount = _to_won(amt_m.group(1))
    if unit_count > 1:
        return (
            f"매매목록에 거래가액 {amount:,}원이 기재되어 있으나, 이는 이 물건을 포함해 "
            f"{unit_count}개 부동산을 묶어 거래한 총액입니다. 이 금액을 이 호수 하나의 "
            f"집값으로 사용하면 안 됩니다."
        )
    return None


# ---------------------------------------------------------------------------
# 1단계: 문서 단위 파싱
# ---------------------------------------------------------------------------

def parse_deed_document(raw_text: str) -> DeedDocument:
    """
    등기부 '한 부'의 텍스트를 파싱한다. 판정 없이 사실만 담는다.
    """
    doc = DeedDocument()

    if not raw_text or not raw_text.strip():
        doc.notes.append("PDF에서 텍스트를 추출하지 못했습니다. 스캔 이미지 PDF일 수 있습니다(OCR 필요).")
        return doc

    text = normalize_text(raw_text)
    structured_owner_rows = _structured_owner_rows(text)
    # 구조화 행은 전체 문서 끝에 붙기 때문에 큰 섹션을 자를 때 본문에 섞이지 않게 제거한다.
    text = "\n".join(line for line in text.splitlines() if not line.startswith(OWNER_ROW_MARKER))
    sections = split_sections(text)
    pyojebu_text = sections.get("표제부", "")
    gapgu_text = sections.get("갑구", "")
    eulgu_text = sections.get("을구", "")
    maemae_text = sections.get("매매목록", "")

    # 문서 종류 분류 (표제부 기준)
    doc.doc_type = classify_deed_type(pyojebu_text)
    doc.inferred_property_type = infer_property_type_from_pyojebu(pyojebu_text, doc.doc_type)
    doc.registry_addresses = extract_registry_addresses(pyojebu_text)
    doc.land_parcels = extract_land_parcels(pyojebu_text)
    doc.joint_collateral_land_parcels = extract_land_parcels(
        eulgu_text, joint_collateral_only=True
    )
    if doc.doc_type == DOC_UNKNOWN:
        doc.notes.append("표제부에서 문서 종류(집합건물/건물/토지)를 판별하지 못했습니다.")

    # 집합건물 전용 체크: 대지권 등기 여부 + '토지 별도등기 있음'
    if doc.doc_type == DOC_COLLECTIVE:
        doc.has_daejigwon = bool(_DAEJIGWON_RE.search(pyojebu_text))
        doc.has_separate_land_registry = has_active_separate_land_registry(pyojebu_text)

    if not gapgu_text:
        doc.notes.append("갑구를 찾지 못했습니다. 서식이 다르거나 텍스트 추출이 불완전할 수 있습니다.")
    if not eulgu_text:
        doc.notes.append("을구를 찾지 못했습니다 (근저당이 아예 없는 경우일 수도 있습니다).")

    # 소유자 (갑구)
    doc.current_owner, doc.owner_names = extract_owners(gapgu_text, structured_owner_rows)
    if not doc.current_owner and gapgu_text:
        doc.notes.append("소유자 이름을 자동 추출하지 못했습니다. 서식/OCR 편차 가능성.")

    # 권리제한 / 신탁 (갑구)
    doc.encumbrance_hits = extract_registry_items(gapgu_text, ENCUMBRANCE_KEYWORDS)
    doc.trust_hits = extract_registry_items(gapgu_text, TRUST_KEYWORDS)

    # 근저당 + 을구 용익물권/주택임차권
    _, doc.mortgage_items = extract_mortgages(eulgu_text)
    doc.land_right_hits = extract_registry_items(eulgu_text, LAND_RIGHT_KEYWORDS)
    doc.tenant_right_hits = extract_registry_items(eulgu_text, HOUSING_LEASE_RIGHT_KEYWORDS)

    # 매매목록 함정 가드
    doc.bulk_sale_warning = detect_bulk_sale_warning(maemae_text)

    return doc


# ---------------------------------------------------------------------------
# 2단계: 문서 병합 (공동담보 dedup)
# ---------------------------------------------------------------------------

def merge_mortgages(documents: list[DeedDocument]) -> tuple[int, list[dict[str, Any]]]:
    """
    여러 부(건물+토지)의 근저당을 병합하되, 토지·건물 공동담보로 양쪽에 기재된
    동일 근저당은 1건으로만 합산한다.

    동일성 판단 키 (우선순위):
      1. 접수번호 (같은 근저당은 접수일자·접수번호가 동일하게 기재됨)
      2. (채권최고액, 근저당권자) — 접수번호 파싱 실패 시 보수적 보조 키

    중복 판정은 '서로 다른 문서 사이'에서만 한다. 같은 문서 안에서는
    extract_mortgages가 이미 순위번호로 중복을 걸렀고, 같은 문서 내 동일 금액
    근저당 2건은 실제로 별개 채권일 수 있으므로 합쳐 버리면 안 된다.
    """
    seen: dict[tuple, int] = {}  # key -> 처음 발견된 문서 index
    merged: list[dict[str, Any]] = []
    total = 0

    for doc_idx, doc in enumerate(documents):
        for m in doc.mortgage_items:
            if m.get("receipt_no"):
                key: tuple = ("receipt", m["receipt_no"])
            elif m.get("creditor"):
                key = ("amt_creditor", m["amount"], m["creditor"])
            else:
                key = ("amt_only", m["amount"], doc_idx)  # 정보 부족 시 dedup하지 않음(보수적)

            first_doc = seen.get(key)
            duplicated = first_doc is not None and first_doc != doc_idx
            if first_doc is None:
                seen[key] = doc_idx

            merged.append({
                **m,
                "doc_type": doc.doc_type,
                "deduplicated": duplicated,
            })
            if m["status"] == "active" and not duplicated:
                total += m["amount"]

    merged.sort(key=lambda x: (x["doc_type"], x["rank"]))
    return total, merged


def _merge_hits(documents: list[DeedDocument], attr: str) -> list[dict[str, Any]]:
    """encumbrance_hits / trust_hits / land_right_hits를 doc_type 표시와 함께 병합."""
    merged: list[dict[str, Any]] = []
    for doc in documents:
        for h in getattr(doc, attr):
            merged.append({**h, "doc_type": doc.doc_type})
    return merged


# ---------------------------------------------------------------------------
# HUG / LH 보증 사전점검
# ---------------------------------------------------------------------------

def assess_guarantee_eligibility(
    result: AnalysisResult,
    *,
    deposit: Optional[int],
    house_price: Optional[int],
    property_type: Optional[str],
    is_collective: bool,
    senior_tenant_deposits: Optional[int],
) -> None:
    """
    HUG 전세보증금반환보증 / LH 전세임대의 '등기부·금액 기반' 요건을 사전 점검한다.

    비집합건물 특이 처리:
      - 토지·건물 공동담보는 '정상적인 담보 형태'이므로 그것만으로 불가 처리하지 않는다.
        (집합건물에서 다른 세대와의 공동담보는 기존대로 즉시 불가)
      - 다가구(MULTI_HOUSEHOLD)는 선순위 임차보증금(다른 세대 보증금)이 부채에
        포함되어야 한다. 이 값은 등기부에 없으므로 입력으로 받아야 하며,
        없으면 가입 가능 여부를 확정하지 않는다(None 유지).

    주의: 여기서 판정하는 것은 등기부와 입력 금액만으로 확인 가능한 요건뿐이다.
    실제 가입/지원 가능 여부는 소득·자산요건, 전입신고·확정일자, 위반건축물 여부,
    면적요건(LH 85㎡ 이하), 임대인 신용 등 추가 심사에 따라 달라진다.
    """
    active_encumbrances = [h for h in result.encumbrance_hits if not h["cancelled"]]
    active_land_rights = [h for h in result.land_right_hits if not h["cancelled"]]
    active_joint = [
        m for m in result.mortgage_items
        if m["status"] == "active" and m["joint_collateral"] and not m.get("deduplicated")
    ]

    # ---------------- 집합건물의 타 세대 공동담보 — 무조건 불가 ----------------
    # 집합건물 한 부 분석에서 발견된 공동담보는 '다른 세대/토지와 함께 잡힌' 근저당이라
    # 채권최고액 전액을 이 세대가 단독 책임지는 구조 → 보수적으로 즉시 불가.
    # 비집합건물의 토지·건물 공동담보는 여기 해당하지 않는다.
    if is_collective and active_joint:
        ranks = ", ".join(str(m["rank"]) for m in active_joint)
        reason = (
            f"순위 {ranks}번 근저당이 다른 세대/토지와 공동담보로 설정되어 있습니다. "
            f"채권최고액 전액을 이 세대가 단독으로 책임지는 구조이므로 공동담보만으로 불가 처리합니다."
        )
        result.hug_eligible = False
        result.hug_reasons.append(reason)
        result.lh_eligible = False
        result.lh_reasons.append(reason)
        return

    if not deposit or not house_price or house_price <= 0:
        result.notes.append(
            "보증금 또는 주택가격이 없어 HUG/LH 사전점검(금액 기준 항목)을 수행하지 못했습니다."
        )
        return

    # ---------------- 선순위채권 산정 ----------------
    senior_liens = result.mortgage_total + result.registered_tenant_deposit_total
    if result.registered_tenant_deposit_total:
        result.notes.append(
            f"등기부상 유효 주택임차권 보증금 {result.registered_tenant_deposit_total:,}원을 "
            "선순위채권에 합산했습니다."
        )

    is_multi_household = (property_type == "MULTI_HOUSEHOLD")
    tenant_deposits_missing = is_multi_household and senior_tenant_deposits is None
    if is_multi_household and senior_tenant_deposits:
        # HUG/LH 모두 단독·다가구는 선순위 임차보증금을 부채에 포함해 계산한다
        senior_liens += senior_tenant_deposits
        result.senior_tenant_deposits_used = senior_tenant_deposits
        result.notes.append(
            f"다가구: 선순위 임차보증금 {senior_tenant_deposits:,}원을 선순위채권에 합산했습니다."
        )

    # ---------------- HUG ----------------
    hug_ok = True
    limit_amount = int(house_price * HUG_LTV_RATIO)

    if house_price > HUG_MAX_HOUSE_PRICE:
        hug_ok = False
        result.hug_reasons.append(
            f"주택가격 {house_price:,}원이 HUG 상한(12억원)을 초과합니다."
        )

    if senior_liens + deposit > limit_amount:
        hug_ok = False
        result.hug_reasons.append(
            f"선순위채권+보증금 {senior_liens + deposit:,}원이 "
            f"주택가격×{HUG_LTV_RATIO:.0%} = {limit_amount:,}원을 초과합니다 "
            f"(초과분 {senior_liens + deposit - limit_amount:,}원)."
        )
    else:
        margin = limit_amount - (senior_liens + deposit)
        result.hug_reasons.append(
            f"선순위채권+보증금 ≤ 주택가격×{HUG_LTV_RATIO:.0%} 충족 (여유 {margin:,}원)."
        )
        if margin < house_price * 0.03:
            result.notes.append(
                "HUG 한도 요건을 아슬아슬하게 충족합니다. 주택가격 산정 방식(시세 vs 공시가 환산)에 "
                "따라 결과가 뒤집힐 수 있으니 취급은행을 통해 정확한 산정가를 확인하세요."
            )

    if senior_liens > house_price * HUG_SENIOR_LIEN_LIMIT:
        hug_ok = False
        result.hug_reasons.append(
            f"선순위채권 {senior_liens:,}원이 주택가격의 {HUG_SENIOR_LIEN_LIMIT:.0%} "
            f"({int(house_price * HUG_SENIOR_LIEN_LIMIT):,}원)를 초과합니다."
        )

    if active_encumbrances:
        hug_ok = False
        kws = ", ".join(sorted({h["keyword"] for h in active_encumbrances}))
        result.hug_reasons.append(f"권리침해 등기({kws})가 있어 가입이 제한됩니다.")

    if result.trust_found:
        result.hug_reasons.append(
            "신탁등기가 있습니다. 임대권한 있는 수탁자와 직접 계약하는 경우에만 가입 가능하니 "
            "신탁원부로 임대권한을 반드시 확인하세요."
        )

    # ---------------- LH ----------------
    lh_ok = True
    lh_debt_limit = LH_DEBT_RATIO_LIMIT_BY_TYPE.get(
        property_type or "", LH_DEBT_RATIO_LIMIT_DEFAULT
    )

    # 조건 1: 부채비율 (총부채/주택가격) ≤ 한도 (기본 90%, 단독·다가구 80%)
    debt_ratio = (senior_liens + deposit) / house_price
    if debt_ratio > lh_debt_limit:
        lh_ok = False
        result.lh_reasons.append(
            f"부채비율 {debt_ratio:.1%}가 LH 한도({lh_debt_limit:.0%}"
            f"{', 단독·다가구 보수 기준' if lh_debt_limit < LH_DEBT_RATIO_LIMIT_DEFAULT else ''})를 초과합니다."
        )
    else:
        result.lh_reasons.append(f"부채비율 {debt_ratio:.1%} ≤ {lh_debt_limit:.0%} 충족.")

    # 조건 2: 선순위 설정최고액 ≤ 주택가격의 50%
    if senior_liens > house_price * LH_SENIOR_LIEN_LIMIT:
        lh_ok = False
        result.lh_reasons.append(
            f"선순위 설정최고액 {senior_liens:,}원이 주택가격의 {LH_SENIOR_LIEN_LIMIT:.0%} "
            f"({int(house_price * LH_SENIOR_LIEN_LIMIT):,}원)를 초과합니다."
        )
    else:
        result.lh_reasons.append(
            f"선순위 설정최고액 ≤ 주택가격×{LH_SENIOR_LIEN_LIMIT:.0%} 충족."
        )

    # 조건 3: 소유권 행사 제한 (압류·가압류·가처분·가등기·경매신청 등) 시 불가
    if active_encumbrances:
        lh_ok = False
        result.lh_reasons.append(
            "압류·가압류·가처분·가등기·경매 등 소유권 행사 제한사항이 있어 "
            "권리분석 통과 및 보증보험 가입이 어렵습니다."
        )

    # 조건 4: 전세권·지상권 등 용익물권 설정 물건은 권리분석 통과가 어려움
    if active_land_rights:
        lh_ok = False
        kws = ", ".join(sorted({h["keyword"] for h in active_land_rights}))
        result.lh_reasons.append(
            f"용익물권({kws})이 설정되어 있어 LH 권리분석 통과가 어렵습니다."
        )

    # ---------------- 다가구 확정 보류 ----------------
    if tenant_deposits_missing:
        reason = (
            "다가구주택은 선순위 임차보증금(다른 세대 보증금 합계)을 부채에 포함해야 하지만 "
            "이 값이 입력되지 않았습니다. 등기부만으로는 확인할 수 없으므로 "
            "(확정일자 부여현황·전입세대 열람 필요) 가입 가능 여부를 확정하지 않습니다."
        )
        result.hug_eligible = None
        result.hug_reasons.append(reason)
        result.lh_eligible = None
        result.lh_reasons.append(reason)
        return

    result.hug_eligible = hug_ok
    result.lh_eligible = lh_ok


# ---------------------------------------------------------------------------
# 위험도 계산
# ---------------------------------------------------------------------------

def _ratio_to_score(ratio: float) -> int:
    """(선순위채권+보증금)/집값 비율을 정책 임계값 앵커 기반 연속 점수(0~85)로 변환. (기존 로직)"""
    anchors = [
        (0.00, 0),
        (0.60, 10),
        (0.90, 55),
        (1.00, 70),
        (1.26, 85),
    ]
    if ratio <= 0:
        return 0
    for (x1, y1), (x2, y2) in zip(anchors, anchors[1:]):
        if ratio <= x2:
            t = (ratio - x1) / (x2 - x1)
            return round(y1 + t * (y2 - y1))
    return anchors[-1][1]


def _set_risk_level_from_score(result: AnalysisResult) -> None:
    """누적 위험 점수에 맞는 등급을 일관되게 설정한다."""
    if result.risk_score >= 70:
        result.risk_level = "DANGER"
    elif result.risk_score >= 45:
        result.risk_level = "WARNING"
    elif result.risk_score >= 20:
        result.risk_level = "CAUTION"
    else:
        result.risk_level = "SAFE"


def estimate_monthly_rent_deposit_recovery(
    result: AnalysisResult,
    *,
    deposit: Optional[int],
    house_price: Optional[int],
    property_type: Optional[str],
    senior_tenant_deposits: Optional[int],
) -> None:
    """월세 보증금의 보수적 예상 회수액과 회수율을 계산한다.

    활성 근저당, 등기부상 유효 주택임차권, 다가구의 입력 선순위 임차보증금을
    보수적 처분가에서 먼저 차감한다. 실제 배당 순위·세금·소액임차인 최우선변제는
    계약일과 지역 등 추가 정보가 필요하므로 이 1차 추정에는 반영하지 않는다.
    """
    if not deposit or deposit <= 0 or not house_price or house_price <= 0:
        result.notes.append(
            "월세 보증금 예상 회수율을 계산하려면 보증금과 주택가격이 모두 필요합니다."
        )
        return

    senior_claims = result.mortgage_total + result.registered_tenant_deposit_total
    if result.registered_tenant_deposit_total:
        result.notes.append(
            f"등기부상 유효 주택임차권 보증금 {result.registered_tenant_deposit_total:,}원을 "
            "월세 보증금 회수 추정의 선순위채권에 합산했습니다."
        )
    if property_type == "MULTI_HOUSEHOLD" and senior_tenant_deposits:
        senior_claims += senior_tenant_deposits
        result.senior_tenant_deposits_used = senior_tenant_deposits
        result.notes.append(
            f"다가구: 선순위 임차보증금 {senior_tenant_deposits:,}원을 "
            "월세 보증금 회수 추정의 선순위채권에 합산했습니다."
        )

    recovery_price = int(house_price * MONTHLY_RENT_RECOVERY_VALUE_RATIO)
    available_amount = max(0, recovery_price - senior_claims)
    recoverable_deposit = min(deposit, available_amount)
    recovery_rate = round((recoverable_deposit / deposit) * 100, 2)

    result.recovery_price_used = recovery_price
    result.estimated_recoverable_deposit = recoverable_deposit
    result.deposit_recovery_rate = recovery_rate
    result.notes.append(
        f"월세 보증금 회수 추정은 주택가격의 {MONTHLY_RENT_RECOVERY_VALUE_RATIO:.0%}를 "
        "보수적 처분가로 가정하고, 선순위채권을 먼저 차감한 1차 시뮬레이션입니다. "
        "실제 경매 낙찰가·세금·대항력·확정일자·최우선변제는 반영하지 않았습니다."
    )

    if recovery_rate >= 100:
        result.flags.append("보수적 월세 보증금 회수 추정상 보증금 전액 범위가 확보됩니다.")
    elif recovery_rate >= 80:
        result.flags.append(
            f"월세 보증금 예상 회수율 {recovery_rate:.1f}% — 가격 하락 또는 추가 선순위채권이 있으면 "
            "보증금 일부를 회수하지 못할 수 있습니다."
        )
    elif recovery_rate >= 50:
        result.flags.append(
            f"월세 보증금 예상 회수율 {recovery_rate:.1f}% — 보증금 손실 가능성이 있어 주의가 필요합니다."
        )
    else:
        result.flags.append(
            f"월세 보증금 예상 회수율 {recovery_rate:.1f}% — 보수적 추정상 보증금 손실 위험이 큽니다."
        )

    recovery_risk = 0
    if recovery_rate < 50:
        recovery_risk = 45
    elif recovery_rate < 80:
        recovery_risk = 25
    elif recovery_rate < 100:
        recovery_risk = 10
    if recovery_risk:
        result.risk_score = min(100, result.risk_score + recovery_risk)
        _set_risk_level_from_score(result)


def compute_risk(
    result: AnalysisResult,
    *,
    deposit: Optional[int],
    property_price: Optional[int],
    is_collective: bool,
    senior_tenant_deposits: Optional[int],
    lease_type: str,
) -> None:
    """
    위험 점수를 계산해 result에 채운다. (점수는 낮을수록 안전)

    비집합건물 추가 항목:
      - 건물·토지 소유자 불일치: +30 (법정지상권/경매 배당 리스크)
      - 토지·건물 공동담보는 정상 형태이므로 가중치 없음 (집합건물의 타 세대 공동담보만 +40)
      - 을구 용익물권(전세권/지상권): +15
    """
    score = 0
    active_encumbrances = [h for h in result.encumbrance_hits if not h["cancelled"]]

    # 1) 핵심 비율 — 근저당 + 등기부상 임차권 보증금 + 입력 선순위 임차보증금
    if property_price and property_price > 0:
        deposit_val = deposit or 0
        senior = (
            result.mortgage_total
            + result.registered_tenant_deposit_total
            + (senior_tenant_deposits or 0)
        )
        ratio = (senior + deposit_val) / property_price
        result.risk_ratio = round(ratio, 4)
        score += _ratio_to_score(ratio)

        if ratio >= 1.0:
            result.flags.append(
                f"선순위채권+보증금이 집값을 초과합니다 (비율 {ratio:.0%}). 매우 위험."
            )
        elif ratio > 0.9:
            if lease_type == "WOLSE":
                result.flags.append(
                    f"선순위채권+보증금 비율 {ratio:.0%} — 월세 보증금 회수 여력이 낮을 수 있습니다."
                )
            else:
                result.flags.append(
                    f"선순위채권+보증금 비율 {ratio:.0%} — HUG/LH 한도(90%)를 초과해 "
                    "보증 가입이 어려울 가능성이 높습니다."
                )
        elif ratio > 0.8:
            if lease_type == "WOLSE":
                result.flags.append(
                    f"선순위채권+보증금 비율 {ratio:.0%} — 월세 보증금 회수 위험을 확인할 구간입니다."
                )
            else:
                result.flags.append(
                    f"선순위채권+보증금 비율 {ratio:.0%} — 90% 한도에 근접한 깡통전세 위험 구간입니다."
                )
        elif ratio > 0.6:
            result.flags.append(f"선순위채권+보증금 비율 {ratio:.0%} (주의 구간).")
        else:
            result.flags.append(f"선순위채권+보증금 비율 {ratio:.0%} (양호한 편).")
    else:
        result.notes.append("집값(property_price)이 없어 핵심 위험 비율을 계산하지 못했습니다.")

    # 2) 권리제한 (가압류/가처분/압류/경매/가등기 등 — 건물·토지 어느 쪽이든)
    if active_encumbrances:
        score += min(30, 15 * len(active_encumbrances))
        kws = ", ".join(sorted({h["keyword"] for h in active_encumbrances}))
        result.flags.append(f"권리제한 등기가 발견되었습니다: {kws}. 계약 전 반드시 확인하세요.")

    # 2-1) 집합건물 타 세대 공동담보 — 강한 위험 가중치 (비집합건물의 토지·건물 공동담보는 제외)
    if is_collective:
        active_joint = [
            m for m in result.mortgage_items
            if m["status"] == "active" and m["joint_collateral"] and not m.get("deduplicated")
        ]
        if active_joint:
            ranks = ", ".join(str(m["rank"]) for m in active_joint)
            score += 40
            guarantee_note = (
                "HUG/LH 보증은 이 사유만으로 불가 처리됩니다."
                if lease_type == "JEONSE"
                else "월세 보증금 회수 추정에서도 채권최고액 전액을 선순위로 반영합니다."
            )
            result.flags.append(
                f"순위 {ranks}번 근저당이 다른 세대/토지와 공동담보로 설정되어 있습니다. "
                f"채권최고액 전액을 이 세대가 단독으로 책임지는 구조라 위험도가 높으며, "
                + guarantee_note
            )

    # 2-2) 건물·토지 소유자 불일치 (비집합건물 교차 검증)
    if result.building_land_owner_match is False:
        score += 30
        result.flags.append(
            "건물 소유자와 토지 소유자가 일치하지 않습니다. 법정지상권·경매 배당 문제로 "
            "이어질 수 있는 고위험 구조이며, 보증기관 권리분석 통과도 어렵습니다."
        )

    # 2-3) 을구 용익물권 (전세권/지상권/지역권)
    active_land_rights = [h for h in result.land_right_hits if not h["cancelled"]]
    if active_land_rights:
        score += 15
        kws = ", ".join(sorted({h["keyword"] for h in active_land_rights}))
        result.flags.append(
            f"용익물권({kws})이 설정되어 있습니다. 선순위 권리로서 보증금 회수에 "
            f"영향을 줄 수 있고, LH 권리분석에서 기피 대상입니다."
        )

    # 2-4) 유효한 주택임차권 — 등기부상 선순위 임차인 권리
    active_tenant_rights = [h for h in result.tenant_right_hits if not h["cancelled"]]
    if active_tenant_rights:
        score += min(30, 15 * len(active_tenant_rights))
        amount = result.registered_tenant_deposit_total
        result.flags.append(
            f"유효한 주택임차권 {len(active_tenant_rights)}건"
            + (f"(등기부상 보증금 합계 {amount:,}원)" if amount else "")
            + "이 있습니다. 선순위 임차인 권리를 계약 전 확인하세요."
        )

    # 3) 신탁
    active_trust = [h for h in result.trust_hits if not h["cancelled"]]
    if active_trust:
        score += 20
        result.flags.append(
            "신탁등기가 있습니다. 소유·처분 권한이 수탁자(신탁회사)에게 있을 수 있어, "
            "계약 상대방이 처분 권한을 가졌는지 신탁원부까지 확인이 필요합니다."
        )

    # 4) 소유자 불일치 (계약 임대인 vs 등기부)
    if result.owner_matches_contract is False:
        score += 25
        result.flags.append(
            "등기부상 소유자와 계약 당사자가 일치하지 않습니다. 대리·위임 관계를 확인하세요."
        )

    result.risk_score = min(100, score)

    _set_risk_level_from_score(result)


# ---------------------------------------------------------------------------
# 3단계: 물건 단위 판정 (상위 진입 함수)
# ---------------------------------------------------------------------------

def _require_docs(result: AnalysisResult, doc_types: list[str], reason: str) -> None:
    """추가 문서 필요 상태를 설정한다 (백엔드 알림 트리거)."""
    result.analysis_status = "NEEDS_MORE_DOCS"
    for dt in doc_types:
        if dt not in result.required_documents:
            result.required_documents.append(dt)
    if reason and reason not in result.required_documents_reason:
        sep = " " if result.required_documents_reason else ""
        result.required_documents_reason += sep + reason


def infer_property_type_from_documents(
    documents: list[DeedDocument],
    collective_docs: list[DeedDocument],
) -> str:
    """
    여러 등기부 문서의 표제부 추정값을 합쳐 물건 유형을 정한다.
    건물/집합건물 문서의 값이 토지 문서보다 우선한다.
    """
    for doc in documents:
        if doc.doc_type in {DOC_COLLECTIVE, DOC_BUILDING} and doc.inferred_property_type:
            return doc.inferred_property_type
    for doc in documents:
        if doc.inferred_property_type:
            return doc.inferred_property_type
    return "COLLECTIVE" if collective_docs else "SINGLE_FAMILY"


def analyze_property(
    documents: list[DeedDocument],
    *,
    property_type: Optional[str] = None,
    lease_type: Optional[str] = None,
    contract_owner: Optional[str] = None,
    submitted_address: Optional[str] = None,
    road_address: Optional[str] = None,
    deposit: Optional[int] = None,
    property_price: Optional[int] = None,
    public_price: Optional[int] = None,
    senior_tenant_deposits: Optional[int] = None,
) -> AnalysisResult:
    """
    파싱된 등기부 문서(1부 이상)를 병합해 물건 단위로 위험도·보증 요건을 판정한다.

    Args:
        documents: parse_deed_document 결과 리스트 (집합건물 1부, 또는 건물 1부 + 토지 N부)
        property_type: APARTMENT / ROW_HOUSE / MULTI_FAMILY / OFFICETEL /
                       SINGLE_FAMILY / MULTI_HOUSEHOLD. None이면 문서 종류로 추정.
        lease_type: JEONSE(전세) / WOLSE(월세). None이면 기존 요청과의 호환을 위해 JEONSE.
        contract_owner: 계약 당사자(임대인) 이름
        submitted_address: 사용자가 제출한 원래 주소. 등기부 표제부 주소와 먼저 비교한다.
        road_address: houseId로 조회한 도로명 주소. 원래 주소 불일치 시 보조 비교에 사용한다.
        deposit: 보증금(원)
        property_price: 확인된 시세(원). 단독/다가구는 토지+건물 일체 가격 하나를 넣는다.
        public_price: 공시가격(원). 집합건물은 공동주택공시가격, 단독/다가구는
            개별주택가격(토지+건물 일체 평가액)을 넣는다.
        senior_tenant_deposits: 다가구 선순위 임차보증금 합계(원).
            등기부에 없는 값이므로 확정일자 부여현황 등으로 확인해 입력해야 한다.
    """
    normalized_lease_type = (lease_type or "JEONSE").strip().upper()
    result = AnalysisResult()
    if normalized_lease_type not in {"JEONSE", "WOLSE"}:
        result.notes.append(
            f"알 수 없는 leaseType({lease_type})은 전세(JEONSE)로 처리했습니다."
        )
        normalized_lease_type = "JEONSE"
    result.lease_type = normalized_lease_type
    result.property_type = property_type
    result.documents = [d.summary() for d in documents]

    registry_addresses: list[str] = []
    for document in documents:
        for address in document.registry_addresses:
            if address not in registry_addresses:
                registry_addresses.append(address)

    if submitted_address:
        matched_address = _find_matching_registry_address(submitted_address, registry_addresses)
        if matched_address:
            result.registry_address = matched_address
            result.address_matches_submission = True
            result.address_match_basis = "SUBMITTED_ADDRESS"
        else:
            matched_address = _find_matching_registry_address(road_address, registry_addresses)
            if matched_address:
                result.registry_address = matched_address
                result.address_matches_submission = True
                result.address_match_basis = "ROAD_ADDRESS"
            elif registry_addresses:
                result.registry_address = ", ".join(registry_addresses)
                result.address_matches_submission = False
                result.address_match_basis = "MISMATCH"
            else:
                result.address_match_basis = "NOT_VERIFIABLE"
                result.notes.append(
                    "표제부에서 주소를 추출하지 못해 제출 주소와 등기부 주소의 일치 여부를 확인하지 못했습니다."
                )

    # ---------------- 문서 구성 파악 ----------------
    valid_docs = [d for d in documents if d.doc_type != DOC_UNKNOWN]
    collective_docs = [d for d in documents if d.doc_type == DOC_COLLECTIVE]
    building_docs = [d for d in documents if d.doc_type == DOC_BUILDING]
    land_docs = [d for d in documents if d.doc_type == DOC_LAND]

    for d in documents:
        result.notes.extend(d.notes)
        if d.bulk_sale_warning:
            result.notes.append(d.bulk_sale_warning)

    if not valid_docs:
        result.notes.append("종류를 판별할 수 있는 등기부가 없습니다. 텍스트 추출 실패 가능성이 높습니다.")
        result.risk_level = "UNKNOWN"
        return result

    # property_type이 없으면 문서로 추정
    if property_type is None:
        property_type = infer_property_type_from_documents(documents, collective_docs)
        result.property_type = property_type
        result.notes.append(f"propertyType 미입력 — PDF 표제부로 {property_type}(으)로 추정했습니다.")

    is_collective = property_type in COLLECTIVE_PROPERTY_TYPES

    # ---------------- 유형·문서 정합성 및 필수 문서 검증 ----------------
    if is_collective:
        if not collective_docs:
            result.notes.append(
                f"부동산 유형은 집합건물({property_type})인데 집합건물 등기부가 없습니다. "
                f"유형 선택이 잘못되었거나 다른 파일이 업로드됐을 수 있습니다."
            )
            if building_docs or land_docs:
                # 실제로는 비집합건물 서류가 온 것 — 문서 기준으로 전환해 분석 계속
                is_collective = False
                result.notes.append("업로드된 문서 종류(일반건물/토지)에 맞춰 비집합건물로 분석합니다.")
        if len(collective_docs) > 1:
            result.notes.append("집합건물 등기부가 2부 이상입니다. 동일 물건인지 확인이 필요합니다.")

        # 집합건물이지만 토지 등기부 확인이 필요한 두 케이스 (PDF를 열어야만 알 수 있음)
        for d in collective_docs:
            if d.has_separate_land_registry:
                _require_docs(result, [DOC_LAND],
                              "표제부에 '토지 별도등기 있음'이 기재되어 있습니다. "
                              "토지 등기부의 근저당·가압류를 별도로 확인해야 합니다.")
                result.flags.append(
                    "집합건물이지만 '토지 별도등기 있음' — 대지권 성립 전 토지에 설정된 권리가 "
                    "남아 있을 수 있어 토지 등기부 확인 없이는 안전 판정이 불가합니다."
                )
            if not d.has_daejigwon:
                _require_docs(result, [DOC_LAND],
                              "표제부에서 대지권 표시를 찾지 못했습니다(대지권 미등기 의심). "
                              "토지 등기부로 대지 지분과 권리관계를 확인해야 합니다.")
                result.flags.append(
                    "대지권 등기가 확인되지 않습니다. 대지권 미등기 집합건물은 토지 권리관계를 "
                    "별도로 확인해야 하며, 보증기관 심사에서도 불리하게 작용합니다."
                )

    if not is_collective:
        # 비집합건물: 건물 1부 + 토지 1부 이상이 있어야 완전한 분석 가능
        if collective_docs and not building_docs:
            result.notes.append(
                f"부동산 유형은 {property_type}인데 집합건물 등기부가 업로드되었습니다. "
                f"유형 선택 오류 가능성 — 집합건물 기준으로 재확인이 필요합니다."
            )
        if not building_docs:
            _require_docs(result, [DOC_BUILDING], "건물 등기부가 제출되지 않았습니다.")
        if not land_docs:
            _require_docs(result, [DOC_LAND],
                          "단독/다가구주택은 토지 등기부가 있어야 토지의 근저당·가압류·지상권을 "
                          "확인할 수 있습니다.")
        joint_land_parcels = {
            parcel
            for document in building_docs
            for parcel in document.joint_collateral_land_parcels
        }
        submitted_land_parcels = {
            parcel
            for document in land_docs
            for parcel in document.land_parcels
        }
        missing_land_parcels = sorted(joint_land_parcels - submitted_land_parcels)
        if missing_land_parcels:
            _require_docs(
                result,
                [DOC_LAND],
                "공동담보 토지 등기부가 누락되었습니다: "
                + ", ".join(missing_land_parcels)
                + ". 각 토지의 근저당·압류·소유자를 확인해야 합니다.",
            )
        if len(building_docs) > 1:
            result.notes.append("건물 등기부가 2부 이상입니다. 동일 물건인지 확인이 필요합니다.")
        # 토지 여러 부(여러 필지)는 정상 케이스이므로 경고하지 않는다.

    # ---------------- 병합 ----------------
    result.mortgage_total, result.mortgage_items = merge_mortgages(documents)
    result.encumbrance_hits = _merge_hits(documents, "encumbrance_hits")
    result.trust_hits = _merge_hits(documents, "trust_hits")
    result.land_right_hits = _merge_hits(documents, "land_right_hits")
    result.tenant_right_hits = _merge_hits(documents, "tenant_right_hits")
    result.trust_found = any(not h["cancelled"] for h in result.trust_hits)
    result.registered_tenant_deposit_total = sum(
        int(h.get("amount") or 0)
        for h in result.tenant_right_hits
        if not h["cancelled"]
    )

    dedup_count = sum(1 for m in result.mortgage_items if m.get("deduplicated"))
    if dedup_count:
        result.notes.append(
            f"토지·건물 공동담보로 양쪽 등기부에 기재된 근저당 {dedup_count}건을 "
            f"중복 합산하지 않고 1건으로 계산했습니다."
        )

    # ---------------- 소유자 (기준 문서: 집합건물 > 건물 > 토지) ----------------
    primary_doc = (collective_docs or building_docs or land_docs or valid_docs)[0]
    result.current_owner = primary_doc.current_owner
    seen_names: list[str] = []
    for d in documents:
        if not d.current_owner:
            continue
        for n in d.current_owner.split(","):
            name = n.strip()
            if name and name not in seen_names:
                seen_names.append(name)
    result.owner_names = seen_names

    # 건물·토지 소유자 교차 검증 (비집합건물)
    if building_docs and land_docs:
        b_owner = building_docs[0].current_owner
        l_owners = {d.current_owner for d in land_docs if d.current_owner}
        if b_owner and l_owners:
            result.building_land_owner_match = all(
                set(o.strip() for o in b_owner.split(",")) == set(o.strip() for o in lo.split(","))
                for lo in l_owners
            )
        elif b_owner or l_owners:
            result.notes.append(
                "건물 또는 토지 등기부의 소유자를 추출하지 못해 건물·토지 소유자 일치 여부를 "
                "확인하지 못했습니다."
            )

    # 계약 임대인 대조 — 비집합건물이면 건물·토지 '모든' 문서의 소유자와 일치해야 함
    if contract_owner:
        norm_contract = contract_owner.strip()
        docs_to_check = collective_docs if is_collective else (building_docs + land_docs)
        docs_with_owner = [d for d in docs_to_check if d.current_owner]
        if docs_with_owner:
            def _match(owner_str: str) -> bool:
                names = [n.strip() for n in owner_str.split(",")]
                return any(
                    norm_contract == n or norm_contract in n or n in norm_contract
                    for n in names
                )
            result.owner_matches_contract = all(_match(d.current_owner) for d in docs_with_owner)

    # ---------------- 주택가격 결정 ----------------
    # 단독/다가구의 개별주택가격은 토지+건물 일체 평가액이므로 유형과 무관하게 단일 가격 사용
    house_price: Optional[int] = None
    if property_price and property_price > 0:
        house_price = property_price
        result.house_price_basis = "입력된 시세/실거래가 기준"
        if public_price and public_price > 0:
            derived = int(public_price * PUBLIC_PRICE_MULTIPLIER)
            if property_price > derived:
                review_subject = "보증기관" if normalized_lease_type == "JEONSE" else "보수적 회수율 추정"
                result.notes.append(
                    f"입력한 시세({property_price:,}원)가 공시가격×{PUBLIC_PRICE_MULTIPLIER:.0%}"
                    f"({derived:,}원)보다 높습니다. {review_subject}에는 더 낮은 산정가가 "
                    "적용될 수 있습니다."
                )
    elif public_price and public_price > 0:
        house_price = int(public_price * PUBLIC_PRICE_MULTIPLIER)
        price_name = "개별주택가격" if not is_collective else "공시가격"
        price_basis = "HUG 산정 방식" if normalized_lease_type == "JEONSE" else "월세 회수율 추정의 가격 기준"
        result.house_price_basis = (
            f"{price_name} {public_price:,}원 × {PUBLIC_PRICE_MULTIPLIER:.0%} 환산 ({price_basis})"
        )
    result.house_price_used = house_price
    if normalized_lease_type == "JEONSE" and deposit is not None and house_price and house_price > 0:
        result.jeonse_rate = round((deposit / house_price) * 100, 2)

    # ---------------- 위험도 ----------------
    compute_risk(
        result,
        deposit=deposit,
        property_price=house_price,
        is_collective=is_collective,
        senior_tenant_deposits=senior_tenant_deposits if property_type == "MULTI_HOUSEHOLD" else None,
        lease_type=normalized_lease_type,
    )

    if normalized_lease_type == "JEONSE":
        # ---------------- HUG / LH 사전점검 ----------------
        assess_guarantee_eligibility(
            result,
            deposit=deposit,
            house_price=house_price,
            property_type=property_type,
            is_collective=is_collective,
            senior_tenant_deposits=senior_tenant_deposits,
        )
    else:
        estimate_monthly_rent_deposit_recovery(
            result,
            deposit=deposit,
            house_price=house_price,
            property_type=property_type,
            senior_tenant_deposits=senior_tenant_deposits,
        )
        result.notes.append(
            "월세 계약에는 현재 전세 기준 HUG/LH 사전점검을 적용하지 않았습니다. "
            "월세 보증 상품 가입 가능 여부는 보증금의 전세환산액과 상품별 요건을 별도로 확인하세요."
        )

    # ---------------- 최종 등급 보정 ----------------
    # (a) 필수 문서 누락: 반쪽 데이터로 SAFE를 내보내는 사고 방지 → 등급 확정하지 않음
    if result.analysis_status == "NEEDS_MORE_DOCS":
        result.notes.append(
            f"추가 등기부({', '.join(result.required_documents)})가 필요해 위험 등급을 "
            f"확정하지 않았습니다. 현재 점수({result.risk_score})는 제출된 문서 기준 참고값입니다."
        )
        result.risk_level = "UNKNOWN"

    # (b) 집값 산정값이 없으면 핵심 비율과 보증 가능 여부를 계산할 수 없다.
    #     기본 점수 0을 SAFE로 오인하지 않도록 최종 등급을 확정하지 않는다.
    elif house_price is None:
        result.notes.append(
            "집값 산정값이 없어 위험 등급을 확정하지 않았습니다. 입력 시세 또는 공시가격을 확인하세요."
        )
        result.risk_level = "UNKNOWN"

    # (b) 다가구인데 선순위 임차보증금 미입력: 실제 부채가 계산보다 클 수 있으므로
    #     SAFE 확정 금지 (최소 CAUTION으로 상향)
    elif property_type == "MULTI_HOUSEHOLD" and senior_tenant_deposits is None:
        result.notes.append(
            "다가구주택의 선순위 임차보증금이 입력되지 않아 실제 부채비율이 계산값보다 "
            "높을 수 있습니다. 확정일자 부여현황·전입세대 열람으로 반드시 확인하세요."
        )
        if result.risk_level == "SAFE":
            result.risk_level = "CAUTION"
            result.flags.append(
                "등기부 기준으로는 양호하나, 다가구 선순위 임차보증금 미확인으로 "
                "SAFE 대신 CAUTION으로 판정합니다."
            )

    # 주소가 다른 등기부는 해당 매물의 권리관계를 보장하지 못한다.
    if result.address_matches_submission is False:
        result.flags.append(
            "제출 주소와 등기부 표제부 주소가 일치하지 않습니다. 다른 물건의 등기부일 수 있어 안전 등급을 확정하지 않습니다."
        )
        result.notes.append(
            f"제출 주소: {submitted_address}. 등기부에서 추출한 주소: {result.registry_address}."
        )
        result.risk_level = "UNKNOWN"

    return result


# ---------------------------------------------------------------------------
# 하위 호환 진입점 (단일 문서)
# ---------------------------------------------------------------------------

def analyze_deed_text(
    raw_text: str,
    *,
    contract_owner: Optional[str] = None,
    submitted_address: Optional[str] = None,
    road_address: Optional[str] = None,
    deposit: Optional[int] = None,
    property_price: Optional[int] = None,
    public_price: Optional[int] = None,
    property_type: Optional[str] = None,
    lease_type: Optional[str] = None,
    senior_tenant_deposits: Optional[int] = None,
) -> AnalysisResult:
    """
    등기부등본 '한 부'의 텍스트를 받아 분석 결과를 반환한다. (하위 호환 래퍼)
    내부적으로 parse_deed_document + analyze_property를 호출한다.
    """
    doc = parse_deed_document(raw_text)
    return analyze_property(
        [doc],
        property_type=property_type,
        lease_type=lease_type,
        contract_owner=contract_owner,
        submitted_address=submitted_address,
        road_address=road_address,
        deposit=deposit,
        property_price=property_price,
        public_price=public_price,
        senior_tenant_deposits=senior_tenant_deposits,
    )


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """PDF 바이트에서 텍스트를 추출한다."""
    import io
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            # '열람용' 워터마크는 일반 본문보다 훨씬 큰 글자다. 이를 먼저 제거하면
            # 주민번호 앞 이름과 접수번호에 워터마크 글자가 합쳐지는 문제를 막을 수 있다.
            clean_page = page.filter(
                lambda obj: obj.get("object_type") != "char" or obj.get("size", 0) <= 15
            )
            parts.append(clean_page.extract_text() or "")

            # 표 선을 인식할 수 있는 PDF는 갑구의 셀 경계를 그대로 보존한다. 일반 텍스트는
            # 페이지/열 순서가 섞일 수 있으므로, 소유자 추출에만 이 구조화 행을 우선 사용한다.
            try:
                tables = clean_page.extract_tables()
            except Exception:
                tables = []
            for table in tables:
                for row in table:
                    if not row or len(row) < 2:
                        continue
                    rank = (row[0] or "").strip()
                    purpose = (row[1] or "").replace("\n", " ").strip()
                    rights = (row[-1] or "").replace("\n", " ").strip()
                    if re.fullmatch(r"\d+(?:-\d+)?", rank) and purpose and rights:
                        parts.append(f"{OWNER_ROW_MARKER}{rank}|{purpose}|{rights}")
    return "\n".join(parts)
