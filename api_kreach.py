"""
api_kreach.py — K-REACH (한국환경공단) 화평법 API 모듈

[실제 API 응답 기반 설계]

sbstnClsfTypeNm 종류와 처리:
  "인체등유해성물질" → contInfo에서 급성/만성/생태 기준% 파싱 → 유해화학물질 판정
  "유독물질"         → 유독 Y (규제컬럼)
  "금지물질"         → 금지 Y (규제컬럼) + excpInfo 보존
  "제한물질"         → 제한 Y (규제컬럼)
  "사고대비물질"     → 사고대비 Y + contInfo는 혼합물기준이므로 유해판정 제외
  "허가물질"         → 허가 Y
  "등록대상기존화학물질" → 등록 Y
  "중점관리물질"     → 중점 Y
  "기존화학물질", "로테르담협약물질" 등 → 무시

contInfo 파싱 (인체등유해성물질에만 적용):
  "인체급성유해성 : 10%, 인체만성유해성 : 0.1%"
  "인체만성유해성 : 0.1%, 생태유해성 : 25%"
  → {급성: 10.0, 만성: 0.1, 생태: 25.0}

유해화학물질 판정:
  MSDS 최대함량(미만 보정 완료) >= 카테고리별 기준% 이면 해당
  여러 카테고리 중 가장 낮은 기준이 적용
"""

import re, time, requests
from functools import lru_cache

# ── 재시도 설정 ───────────────────────────────────────
_MAX_RETRIES  = 3    # 최대 재시도 횟수
_RETRY_DELAY  = 2.0  # 첫 재시도 대기(초) — 이후 2배씩 증가

def _get_with_retry(url: str, params: dict) -> requests.Response:
    """
    429 Too Many Requests 발생 시 지수 백오프(Exponential Backoff)로 재시도.
    그 외 오류는 즉시 raise.
    """
    delay = _RETRY_DELAY
    for attempt in range(_MAX_RETRIES):
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 429:
            if attempt < _MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2   # 2초 → 4초 → 8초
                continue
            # 마지막 재시도도 429면 raise
            r.raise_for_status()
        else:
            r.raise_for_status()
            return r
    return r  # 안전망

KREACH_URL = "https://apis.data.go.kr/B552584/kecoapi/ncissbstn/chemSbstnList"
KREACH_KEY = "74900080d62ab5a2e048a48ceb2da648d2399701bc1670908b414633f3838af4"

# ── 규제 컬럼 매핑 ────────────────────────────────────
TYPE_TO_COL = {
    "유독물질":             "유독",
    "허가물질":             "허가",
    "제한물질":             "제한",
    "금지물질":             "금지",
    "사고대비물질":         "사고대비",
    "등록대상기존화학물질":  "등록",
    "등록대상":             "등록",
    "중점관리물질":         "중점",
}

# ── 인체등유해성물질 contInfo 파싱 키워드 ─────────────
# "인체급성유해성 : 10%" → 급성 10.0
# "인체만성유해성 : 0.1%" → 만성 0.1
# "생태유해성 : 25%" → 생태 25.0
HAZARD_PARSE_KW = [
    (r"인체\s*급성",  "급성"),
    (r"급성\s*유해",  "급성"),
    (r"인체\s*만성",  "만성"),
    (r"만성\s*유해",  "만성"),
    (r"생태\s*유해",  "생태"),
    (r"수생\s*유해",  "생태"),
]

# ── 사고대비물질 contInfo → 혼합물 함량 기준 파싱 ──────
# "톨루엔 및 이를 85% 이상 함유한 혼합물" → 85%
# (유해판정과는 별개 — 사고대비물질 기준 표시용)

EMPTY = {
    "고유번호":         "—",
    "화학물질명_국문":  "—",
    "화학물질명_영문":  "—",
    "기존화학_ke":      "",
    "고시일자":         "",
    "유독":        "N",
    "허가":        "N",
    "제한":        "N",
    "금지":        "N",
    "사고대비":    "N",
    "등록":        "N",
    "중점":        "N",
    "유해판정":        None,
    "유해_적용기준":   "",
    "유해_기준표":     [],
    "유해물질_고유번호":  "",
    "유해물질_예외조건":  "",
    "분류상세":        [],
}


def _parse_hazard_continfo(cont_info: str) -> dict[str, float]:
    """
    "인체등유해성물질" contInfo에서 카테고리별 기준% 파싱.
    예: "인체급성유해성 : 10%, 인체만성유해성 : 0.1%"
    → {"급성": 10.0, "만성": 0.1}
    """
    result: dict[str, float] = {}
    if not cont_info:
        return result

    # 세미콜론 또는 쉼표로 구분된 각 조각 처리
    # 예: "인체급성유해성 : 10%, 인체만성유해성 : 0.1%"
    # 콤마 뒤에 공백이 있는 경우 분리
    segments = re.split(r",\s*(?=[가-힣])", cont_info)

    for seg in segments:
        num_m = re.search(r":\s*(\d+(?:\.\d+)?)\s*%", seg)
        if not num_m:
            continue
        val = float(num_m.group(1))
        for pattern, cat in HAZARD_PARSE_KW:
            if re.search(pattern, seg):
                # 같은 카테고리에서 더 낮은 기준 우선
                if cat not in result or val < result[cat]:
                    result[cat] = val
                break

    return result


def _parse_accident_threshold(cont_info: str) -> str:
    """
    사고대비물질 contInfo에서 혼합물 기준 추출 (표시용).
    "톨루엔 및 이를 85% 이상 함유한 혼합물" → "85% 이상"
    """
    if not cont_info:
        return ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(이상|초과|미만|이하)?", cont_info)
    return f"{m.group(1)}% {m.group(2) or ''}".strip() if m else cont_info[:40]

@lru_cache(maxsize=3000)
def fetch_kreach_raw_by_cas(cas_no: str) -> dict:
    """
    CAS 번호 기준으로 K-REACH 원천 데이터만 조회/정리.
    함량 판정은 하지 않음.
    """
    row = {**EMPTY}

    if not cas_no or cas_no.strip() in ("", "—"):
        row["유해_적용기준"] = "CAS번호 없음"
        return row

    params = {
        "serviceKey":  KREACH_KEY,
        "searchGubun": "2",
        "searchNm":    cas_no.strip(),
        "pageNo":      "1",
        "numOfRows":   "5",
        "returnType":  "JSON",
    }

    try:
        r = _get_with_retry(KREACH_URL, params)
        body  = r.json().get("body", {})
        items = body.get("items", [])

        if isinstance(items, dict):
            items = [items]

        if not items:
            row["not_found"] = True
            row["유해_적용기준"] = "K-REACH DB 미등록"
            return row

        item = items[0]
        row["고유번호"]       = (item.get("korexst") or "").strip() or "—"
        row["화학물질명_국문"] = (item.get("sbstnNmKor") or "").strip() or "—"
        row["화학물질명_영문"] = (item.get("sbstnNmEng") or "").strip() or "—"

        type_list = item.get("typeList", [])
        if isinstance(type_list, dict):
            type_list = [type_list]

        hazard_criteria: dict[str, float] = {}
        details = []
        기존화학_ke = ""
        고시일자 = ""
        유해물질_고유번호 = ""
        유해물질_예외조건 = ""
        사고대비_기준 = ""
        금지_근거 = ""

        for t in type_list:
            nm   = t.get("sbstnClsfTypeNm", "")
            cont = (t.get("contInfo") or "").strip()
            excp = (t.get("excpInfo") or "").strip()
            unq  = (t.get("unqNo") or "").strip()
            anc  = (t.get("ancmntInfo") or "").strip()
            ymd  = (t.get("ancmntYmd") or "").strip()

            details.append({
                "유형": nm,
                "고유번호": unq,
                "함량기준": cont,
                "예외정보": excp,
                "고시": anc,
            })

            for key, col in TYPE_TO_COL.items():
                if key in nm:
                    row[col] = "Y"
                    break

            if "인체등유해성물질" in nm:
                parsed = _parse_hazard_continfo(cont)
                for cat, thr in parsed.items():
                    if cat not in hazard_criteria or thr < hazard_criteria[cat]:
                        hazard_criteria[cat] = thr
                if ymd and ymd.strip():
                    y = ymd.strip()
                    고시일자 = f"{y[:4]}-{y[4:6]}-{y[6:]}" if len(y) == 8 else y
                if unq:
                    유해물질_고유번호 = unq
                if excp:
                    유해물질_예외조건 = excp

            if "기존화학물질" in nm and unq:
                기존화학_ke = unq

            if "금지물질" in nm:
                if excp:
                    금지_근거 = excp
                if unq and not 유해물질_고유번호:
                    유해물질_고유번호 = unq

            if "사고대비물질" in nm and cont:
                사고대비_기준 = _parse_accident_threshold(cont)
                if unq and not 유해물질_고유번호:
                    유해물질_고유번호 = unq

            if nm in ("유독물질", "허가물질", "제한물질") and unq and not 유해물질_고유번호:
                유해물질_고유번호 = unq

        row["분류상세"] = details
        row["기존화학_ke"] = 기존화학_ke
        row["고시일자"] = 고시일자
        row["유해물질_고유번호"] = 유해물질_고유번호
        row["유해물질_예외조건"] = 유해물질_예외조건
        row["사고대비_기준"] = 사고대비_기준
        row["금지_근거"] = 금지_근거
        row["_hazard_criteria"] = hazard_criteria

        return row

    except Exception as e:
        row["error"] = str(e)
        row["유해_적용기준"] = f"조회 오류: {e}"
        return row

def apply_kreach_judgment(raw_row: dict,
                          content_max: float | None = None,
                          is_lt: bool = False) -> dict:
    """
    fetch_kreach_raw_by_cas() 결과에 대해 함량 판정만 수행.
    외부 API 호출 없음.
    """
    row = dict(raw_row)

    if row.get("error") or row.get("not_found"):
        return row

    hazard_criteria = row.pop("_hazard_criteria", {}) or {}

    # 1~4: 함량 무관 즉시 유해화학물질
    for col, label in [("금지","금지물질"), ("유독","유독물질"),
                       ("허가","허가물질"), ("제한","제한물질")]:
        if row.get(col) == "Y":
            row.update({
                "유해판정": "Y",
                "유해_적용기준": f"{label} 해당 (함량 무관, 화관법 §2)",
                "유해_기준표": [],
            })
            return row

    # 사고대비물질
    if row.get("사고대비") == "Y":
        acc_thr_str = row.get("사고대비_기준", "")
        acc_m = re.search(r"(\d+(?:\.\d+)?)", acc_thr_str)
        if acc_m:
            acc_thr = float(acc_m.group(1))
            if content_max is None:
                row.update({
                    "유해판정": None,
                    "유해_적용기준": f"사고대비물질 | 혼합물기준 {acc_thr}% | MSDS 함량 불명확",
                    "유해_기준표": [{"카테고리":"사고대비","기준값":acc_thr,"MSDS최대":None,"초과":None}],
                })
            elif content_max >= acc_thr:
                row.update({
                    "유해판정": "Y",
                    "유해_적용기준": f"사고대비물질 | 혼합물기준 {acc_thr}% | MSDS 최대함량 {content_max}% → 초과",
                    "유해_기준표": [{"카테고리":"사고대비","기준값":acc_thr,"MSDS최대":content_max,"초과":True}],
                })
            else:
                row.update({
                    "유해판정": "N",
                    "유해_적용기준": f"사고대비물질 | 혼합물기준 {acc_thr}% | MSDS 최대함량 {content_max}% → 미달",
                    "유해_기준표": [{"카테고리":"사고대비","기준값":acc_thr,"MSDS최대":content_max,"초과":False}],
                })
        else:
            row.update({
                "유해판정": "Y",
                "유해_적용기준": "사고대비물질 해당 (혼합물기준 불명확, 보수적 처리)",
                "유해_기준표": [],
            })
        return row

    # 인체등유해성물질
    judgment = judge_hazardous(hazard_criteria, content_max)
    row.update(judgment)
    return row

def judge_hazardous(hazard_criteria: dict[str, float],
                    content_max: float | None) -> dict:
    """
    인체등유해성물질 기준표 + MSDS 최대함량으로 유해화학물질 판정.
    hazard_criteria: {"급성": 10.0, "만성": 0.1, "생태": 25.0}
    content_max: 미만 보정(-0.01) 완료된 최대함량(%)
    """
    if not hazard_criteria:
        return {
            "유해판정":      "N",
            "유해_적용기준": "유해화학물질 해당 분류 없음 (인체등유해성물질 미지정)",
            "유해_기준표":   [],
        }
    if content_max is None:
        return {
            "유해판정":      None,
            "유해_적용기준": "함량 불명확 (MSDS 파싱 실패)",
            "유해_기준표":   [],
        }

    criteria_table = []
    triggered = []

    for cat, thr in sorted(hazard_criteria.items(), key=lambda x: x[1]):
        exceeded = content_max >= thr
        criteria_table.append({
            "카테고리": cat,
            "기준값":   thr,
            "MSDS최대": content_max,
            "초과":     exceeded,
        })
        if exceeded:
            triggered.append((thr, cat))

    if triggered:
        triggered.sort(key=lambda x: x[0])   # 가장 낮은 기준
        low_thr, low_cat = triggered[0]
        basis = (
            f"인체등유해성물질 | {low_cat} 기준 {low_thr}% | "
            f"MSDS 최대함량 {content_max}% → 초과"
        )
        return {
            "유해판정":      "Y",
            "유해_적용기준": basis,
            "유해_기준표":   criteria_table,
        }
    else:
        min_thr = min(hazard_criteria.values())
        basis = (
            f"인체등유해성물질 | 최소기준 {min_thr}% | "
            f"MSDS 최대함량 {content_max}% → 미달"
        )
        return {
            "유해판정":      "N",
            "유해_적용기준": basis,
            "유해_기준표":   criteria_table,
        }


def search_by_cas(cas_no: str,
                  content_max: float | None = None,
                  is_lt: bool = False) -> dict:
    raw_row = fetch_kreach_raw_by_cas(cas_no)
    return apply_kreach_judgment(raw_row, content_max, is_lt)
