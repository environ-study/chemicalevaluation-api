"""
app.py — 화학물질 직접입력 분석 서버 (Railway 배포용)
GPT/OCR 기능 제거 — K-REACH · KOSHA 직접입력 분석만 유지
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import concurrent.futures, traceback, os, threading, time

app = Flask(__name__)
CORS(app)

# ── 모듈 임포트 ──────────────────────────────────────
_import_errors = {}

try:
    from api_kreach import (
    search_by_cas as kreach_search,
    fetch_kreach_raw_by_cas,
    apply_kreach_judgment,
    KREACH_URL,
    KREACH_KEY,
)
except Exception as e:
    _import_errors["api_kreach"] = str(e)
    kreach_search = None; KREACH_URL = ""; KREACH_KEY = ""

try:
    from api_kosha import search_by_cas as kosha_search, KOSHA_URL, KOSHA_KEY
except Exception as e:
    _import_errors["api_kosha"] = str(e)
    kosha_search = None; KOSHA_URL = ""; KOSHA_KEY = ""

try:
    import requests as _req
except Exception as e:
    _import_errors["requests"] = str(e)
    _req = None

# ── 전역 조회 캐시 ──────────────────────────────────
# KOSHA: CAS만 같으면 결과 거의 고정
_KOSHA_CACHE = {}
_KOSHA_CACHE_LOCK = threading.Lock()

# K-REACH: 현재 로직상 함량/미만여부까지 포함해서 결과 캐시
_KREACH_CACHE = {}
_KREACH_CACHE_LOCK = threading.Lock()

# 캐시 최대 보관 개수
_CACHE_MAX = 3000

# ── 전역 에러 핸들러 ──────────────────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"error": str(e), "trace": traceback.format_exc()[-800:]}), 500

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": f"엔드포인트 없음: {request.path}"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": f"허용되지 않은 메서드: {request.method}"}), 405


# ══════════════════════════════════════════════
#  상태 확인
# ══════════════════════════════════════════════
@app.route("/")
def index():
    status = {
        "status": "ok",
        "services": {
            "kreach": "정상" if not _import_errors.get("api_kreach") else f"⚠️ {_import_errors['api_kreach']}",
            "kosha":  "정상" if not _import_errors.get("api_kosha")  else f"⚠️ {_import_errors['api_kosha']}",
        }
    }
    if _import_errors:
        status["import_errors"] = _import_errors
    return jsonify(status)


@app.route("/api/debug")
def debug():
    return jsonify({
        "python":        __import__("sys").version,
        "cwd":           os.getcwd(),
        "import_errors": _import_errors,
        "kreach_url":    KREACH_URL,
        "kosha_url":     KOSHA_URL,
        "cache": {
            "kreach_size": len(_KREACH_CACHE),
            "kosha_size": len(_KOSHA_CACHE),
            "cache_max": _CACHE_MAX,
        }
    })

def _trim_cache(cache: dict, max_size: int):
    """단순 FIFO 방식 캐시 정리"""
    if len(cache) <= max_size:
        return
    remove_count = len(cache) - max_size
    for i, k in enumerate(list(cache.keys())):
        if i >= remove_count:
            break
        cache.pop(k, None)


# ── 클라이언트 전송 전 불필요 필드 제거 ──────────────────
# 프론트엔드가 실제로 사용하는 K-REACH 필드:
#   화학물질명_국문/영문, not_found, error, 금지, 유독, 허가, 제한,
#   사고대비, 사고대비_기준, 중점, 유해_기준표, 기존화학_ke,
#   고시일자, 유해물질_고유번호, 유해물질_예외조건, 유해판정, 금지_근거
#
# 제거 대상 (프론트 미사용):
#   분류상세 — 서버 내부 처리용, 약 300~500 bytes/물질
#   유해_적용기준 — 긴 설명 문자열, 렌더링 미사용
#   고유번호 — 최상위 KE번호, 프론트는 유해물질_고유번호만 사용
#   등록 — 프론트 미사용
#
# 프론트엔드가 실제로 사용하는 KOSHA 필드:
#   금지, 특별관리, not_found, error, chemNameKor
#
# 제거 대상 (프론트 미사용):
#   _all — 20개짜리 itemDetail 배열, 약 1,200~1,500 bytes/물질 (최대 낭비)
#   _list — 목록 원문 응답 (이미 제거 예정)
#   chemId — 프론트 미사용

_KR_REMOVE = {"분류상세", "유해_적용기준", "고유번호", "등록"}
_KO_REMOVE = {"_all", "_list", "chemId"}

def _clean_comp(comp: dict) -> dict:
    """구성성분 dict에서 클라이언트 불필요 필드 제거"""
    kr = {k: v for k, v in comp.get("kreach", {}).items() if k not in _KR_REMOVE}
    ko = {k: v for k, v in comp.get("kosha",  {}).items() if k not in _KO_REMOVE}
    return {**comp, "kreach": kr, "kosha": ko}


def cached_kosha_search(cas: str) -> dict:
    if not kosha_search:
        return {"error": "KOSHA 없음"}

    key = cas.strip()
    with _KOSHA_CACHE_LOCK:
        if key in _KOSHA_CACHE:
            return dict(_KOSHA_CACHE[key])

    result = kosha_search(key)

    with _KOSHA_CACHE_LOCK:
        _KOSHA_CACHE[key] = dict(result)
        _trim_cache(_KOSHA_CACHE, _CACHE_MAX)

    return dict(result)

def cached_kreach_search(cas: str, c_max, is_lt: bool) -> dict:
    # 1) CAS 원천조회는 api_kreach 내부 lru_cache 사용
    raw = fetch_kreach_raw_by_cas(cas.strip())

    # 2) 함량 판정 결과만 별도 전역 캐시
    key = (cas.strip(), c_max, is_lt)

    with _KREACH_CACHE_LOCK:
        if key in _KREACH_CACHE:
            return dict(_KREACH_CACHE[key])

    result = apply_kreach_judgment(raw, c_max, is_lt)

    with _KREACH_CACHE_LOCK:
        _KREACH_CACHE[key] = dict(result)
        _trim_cache(_KREACH_CACHE, _CACHE_MAX)

    return dict(result)

# ══════════════════════════════════════════════
#  직접 입력 분석 (CAS + 함량 직접 입력)
# ══════════════════════════════════════════════
@app.route("/api/manual/analyze", methods=["POST"])
def manual_analyze():
    if kreach_search is None:
        return jsonify({"error": f"api_kreach 로드 실패: {_import_errors.get('api_kreach','')}"}), 500

    data = request.get_json(force=True) or {}

    # 요청 1회 기준 중복 제거용 캐시
    local_cache = {}
    local_cache_lock = threading.Lock()

    def enrich_once(comp: dict) -> dict:
        cas = comp.get("cas_no", "").strip()
        c_max = comp.get("함량최대")
        max_included = bool(comp.get("최대포함여부", True))
        is_lt = not max_included

        # 요청 내 캐시 키
        local_key = (cas, c_max, max_included)

        with local_cache_lock:
            if local_key in local_cache:
                cached = local_cache[local_key]
                comp["kreach"] = dict(cached["kreach"])
                comp["kosha"] = dict(cached["kosha"])
                return comp

        try:
            if kosha_search:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as inner_ex:
                    f_kr = inner_ex.submit(cached_kreach_search, cas, c_max, is_lt)
                    f_ko = inner_ex.submit(cached_kosha_search, cas)
                    kr = f_kr.result()
                    ko = f_ko.result()
            else:
                kr = cached_kreach_search(cas, c_max, is_lt)
                ko = {"error": "KOSHA 없음"}

        except Exception as e:
            kr = {"error": str(e)}
            ko = {"error": str(e)}

        with local_cache_lock:
            local_cache[local_key] = {
                "kreach": dict(kr),
                "kosha": dict(ko),
            }

        comp["kreach"] = kr
        comp["kosha"] = ko
        return comp

    def enrich_comps_parallel(comps: list[dict]) -> list[dict]:
        if not comps:
            return []

        # 입력 수에 따라 적절히 병렬 수 조절
        max_workers = min(8, max(1, len(comps)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            return list(ex.map(enrich_once, comps))

    # 새 구조: {sheets: [{filename, comps}, ...]}
    # 구 구조 호환: {comps: [...]}
    if "sheets" in data:
        results = []
        for sheet in data["sheets"]:
            filename = sheet.get("filename", "직접 입력")
            comps = [_clean_comp(c) for c in enrich_comps_parallel(sheet.get("comps", []))]
            results.append({
                "filename": filename,
                "section2": {},
                "section3": comps
            })
    else:
        comps = [_clean_comp(c) for c in enrich_comps_parallel(data.get("comps", []))]
        results = [{
            "filename": "직접 입력",
            "section2": {},
            "section3": comps
        }]

    if not results:
        return jsonify({"error": "입력 데이터 없음"}), 400

    return jsonify({
        "ok": True,
        "count": len(results),
        "results": results
    })

# ══════════════════════════════════════════════
#  K-REACH 단독 검색
# ══════════════════════════════════════════════
@app.route("/api/chem", methods=["GET"])
def chem_search():
    if not _req:
        return jsonify({"error": "requests 모듈 없음"}), 500
    search_nm = request.args.get("searchNm", "")
    if not search_nm:
        return jsonify({"error": "searchNm 필요"}), 400
    params = {
        "serviceKey":  KREACH_KEY,
        "searchGubun": request.args.get("searchGubun", "2"),
        "searchNm":    search_nm,
        "pageNo":      request.args.get("pageNo", "1"),
        "numOfRows":   request.args.get("numOfRows", "20"),
        "returnType":  "JSON",
    }
    try:
        r = _req.get(KREACH_URL, params=params, timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ══════════════════════════════════════════════
#  KOSHA 단독 검색 (진단용)
# ══════════════════════════════════════════════
@app.route("/api/kosha/search", methods=["GET"])
def kosha_search_route():
    cas = request.args.get("cas", "").strip()
    if not cas:
        return jsonify({"error": "cas 파라미터 필요"}), 400
    if not kosha_search:
        return jsonify({"error": f"api_kosha 로드 실패: {_import_errors.get('api_kosha','')}"}), 500
    try:
        result = kosha_search(cas)
        return jsonify({"ok": True, "cas": cas, "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ══════════════════════════════════════════════
#  실행 (Railway는 PORT 환경변수 자동 지정)
# ══════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("  화학물질 직접입력 분석 서버")
    print(f"  http://0.0.0.0:{port}")
    if _import_errors:
        print("  ⚠️  임포트 에러:")
        for mod, err in _import_errors.items():
            print(f"      {mod}: {err}")
    print("  POST /api/manual/analyze  ← 직접입력 분석")
    print("  GET  /api/chem            ← K-REACH 검색")
    print("  GET  /api/kosha/search    ← KOSHA 검색")
    print("  GET  /api/debug           ← 진단 정보")
    print("=" * 55)
    app.run(debug=False, host="0.0.0.0", port=port)
