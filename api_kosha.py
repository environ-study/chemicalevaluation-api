"""
api_kosha.py — KOSHA (안전보건공단) MSDS API 모듈

[API 구조]
  Base URL: https://apis.data.go.kr/B552468/msdschemAPI
  1단계: /getChemList   → CAS번호로 chemId 조회
  2단계: /getChemDetail15 → chemId로 법적 규제현황 조회
         (15. 법적 규제현황 — 금지물질·특별관리물질 포함)

[확인 항목]
  금지물질     : 산안법 §117
  특별관리물질 : 산안법 시행규칙 [별표21]
"""

import requests
import xml.etree.ElementTree as ET

KOSHA_URL = "https://apis.data.go.kr/B552468/msdschem"
KOSHA_KEY = "74900080d62ab5a2e048a48ceb2da648d2399701bc1670908b414633f3838af4"

EMPTY_ROW = {
    "chemId":      "—",
    "chemNameKor": "—",
    "금지":        "—",
    "특별관리":    "—",
}


def _parse_response(r: requests.Response) -> list[dict]:
    """
    실제 KOSHA API 응답 파싱.
    XML 구조: <response><body><items><item>...</item></items></body></response>
    """
    text = r.text.strip()
    ct   = r.headers.get("Content-Type", "")

    # JSON 응답
    if text.startswith("{") or "json" in ct.lower():
        data  = r.json()
        items = (data.get("response", {}).get("body", {}).get("items", {}) or
                 data.get("body", {}).get("items", {}))
        if isinstance(items, dict):
            items = items.get("item", [])
        if isinstance(items, dict):
            items = [items]
        return [{k: str(v) for k, v in item.items()} for item in (items or [])]

    # XML 응답 (실제 구조 기준)
    try:
        # 공공데이터포털은 응답에 <script/> 태그 포함 — 그냥 파싱하면 됨
        # 단, 인코딩 선언이 없는 경우 content 대신 text 사용
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            # content 실패 시 text로 재시도
            root = ET.fromstring(text.encode("utf-8"))

        nodes = root.findall(".//item")
        result = []
        for node in nodes:
            d = {}
            for c in node:
                d[c.tag] = (c.text or "").strip()
            if d:
                result.append(d)
        return result
    except ET.ParseError as e:
        raise ET.ParseError(f"XML 파싱 실패: {e}\n응답: {text[:300]}")


def _get(endpoint: str, params: dict) -> list[dict]:
    """Base URL + endpoint 호출 후 item 목록 반환."""
    url = KOSHA_URL + endpoint
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return _parse_response(r)


def _xt(el, *tags) -> str:
    """XML element에서 태그 텍스트 추출 (app.py 호환용)."""
    for tag in tags:
        try:
            node = el.find(tag)
            if node is not None and node.text and node.text.strip():
                return node.text.strip()
        except Exception:
            pass
    return ""


def _all_tags(el) -> dict:
    """XML element의 모든 자식 태그 → dict (app.py 호환용)."""
    return {c.tag: (c.text or "").strip() for c in el}


def _yn(d: dict, *keys) -> str:
    """dict에서 여러 후보 키 중 첫 번째로 Y/N 반환."""
    for k in keys:
        v = (d.get(k) or "").strip().upper()
        if v in ("Y", "N"):
            return v
    return "—"


def _txt(d: dict, *keys) -> str:
    """dict에서 여러 후보 키 중 첫 번째 비어있지 않은 값 반환."""
    for k in keys:
        v = (d.get(k) or "").strip()
        if v:
            return v
    return "—"


def search_by_cas(cas_no: str) -> dict:
    """
    CAS 번호로 KOSHA 산안법 금지물질·특별관리물질 여부 조회.
    반환: {chemId, chemNameKor, 금지, 특별관리, _all?, error?}

    실제 getChemList 응답 필드:
      casNo, chemId, chemNameKor, enNo, keNo,
      koshaConfirm, lastDate, openYn, unNo
    → 금지/특별관리 여부는 상세 조회 필요
    """
    if not cas_no or cas_no.strip() in ("", "—"):
        return {**EMPTY_ROW, "error": "CAS번호 없음"}

    cas_clean = cas_no.strip()

    try:
        # ── Step 1: CAS → chemId ──────────────────────
        list_items = _get("/getChemList", {
            "serviceKey": KOSHA_KEY,
            "searchWrd":  cas_clean,
            "searchCnd":  "1",       # 1 = CAS번호 검색
            "numOfRows":  "10",
            "pageNo":     "1",
        })

        if not list_items:
            return {**EMPTY_ROW, "not_found": True}

        # 정확한 CAS 매칭 (유사 CAS 제거)
        exact = next((i for i in list_items if i.get("casNo","").strip() == cas_clean), None)
        item0 = exact or list_items[0]

        chem_id   = item0.get("chemId", "").strip()
        chem_name = item0.get("chemNameKor", "").strip() or "—"

        if not chem_id:
            return {**EMPTY_ROW, "not_found": True, "_list": item0}

        chem_id_padded = chem_id.zfill(6)

        # ── Step 2: getChemDetail15 → 15절 법적 규제현황 ──
        is_prohibited = "—"
        is_special    = "—"
        detail_raw    = {}

        try:
            detail_items = _get("/getChemDetail15", {
                "serviceKey": KOSHA_KEY,
                "chemId":     chem_id_padded,
            })
            if detail_items:
                # 모든 item의 itemDetail을 합쳐서 규제 텍스트 전체 확인
                full_text = " | ".join(
                    item.get("itemDetail", "") for item in detail_items
                )
                detail_raw = {"_full_text": full_text, "_items": detail_items}

                # 금지물질: "제조등금지물질" 또는 "금지물질" 포함 여부
                is_prohibited = "Y" if any(
                    kw in full_text for kw in ["제조등금지물질", "금지물질", "제조·사용금지"]
                ) else "N"

                # 특별관리물질: "특별관리물질" 포함 여부
                is_special = "Y" if "특별관리물질" in full_text else "N"

        except Exception as e:
            detail_raw = {"_error": str(e)}

        return {
            "chemId":      chem_id_padded,
            "chemNameKor": chem_name,
            "금지":        is_prohibited,
            "특별관리":    is_special,
            "_list":       item0,       # 디버그: 목록 응답
            "_all":        detail_raw,  # 디버그: 상세 응답 (실제 필드명 확인용)
        }

    except requests.exceptions.Timeout:
        return {**EMPTY_ROW, "error": "KOSHA API 응답 시간 초과"}
    except ET.ParseError as e:
        return {**EMPTY_ROW, "error": f"XML 파싱 실패: {e}"}
    except Exception as e:
        return {**EMPTY_ROW, "error": str(e)}
