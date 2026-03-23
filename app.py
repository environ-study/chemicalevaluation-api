"""
app.py — 화학물질 직접입력 분석 서버 (Railway 배포용)
GPT/OCR 기능 제거 — K-REACH · KOSHA 직접입력 분석만 유지
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import concurrent.futures, traceback, os

app = Flask(__name__)
CORS(app)

# ── 모듈 임포트 ──────────────────────────────────────
_import_errors = {}

try:
    from api_kreach import search_by_cas as kreach_search, KREACH_URL, KREACH_KEY
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
    })


# ══════════════════════════════════════════════
#  직접 입력 분석 (CAS + 함량 직접 입력)
# ══════════════════════════════════════════════
@app.route("/api/manual/analyze", methods=["POST"])
def manual_analyze():
    if kreach_search is None:
        return jsonify({"error": f"api_kreach 로드 실패: {_import_errors.get('api_kreach','')}"}), 500

    data = request.get_json(force=True) or {}

    def enrich(comp: dict) -> dict:
        cas   = comp.get("cas_no", "").strip()
        c_max = comp.get("함량최대")
        is_lt = not bool(comp.get("최대포함여부", True))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                f_kr = ex.submit(kreach_search, cas, c_max, is_lt)
                f_ko = ex.submit(kosha_search, cas) if kosha_search else None
                comp["kreach"] = f_kr.result()
                comp["kosha"]  = f_ko.result() if f_ko else {"error": "KOSHA 없음"}
        except Exception as e:
            comp["kreach"] = {"error": str(e)}
            comp["kosha"]  = {"error": str(e)}
        return comp

    # 새 구조: {sheets: [{filename, comps}, ...]}
    # 구 구조 호환: {comps: [...]}
    if "sheets" in data:
        results = []
        for sheet in data["sheets"]:
            filename = sheet.get("filename", "직접 입력")
            comps    = [enrich(c) for c in sheet.get("comps", [])]
            results.append({"filename": filename, "section2": {}, "section3": comps})
    else:
        comps = [enrich(c) for c in data.get("comps", [])]
        results = [{"filename": "직접 입력", "section2": {}, "section3": comps}]

    if not results:
        return jsonify({"error": "입력 데이터 없음"}), 400

    return jsonify({"ok": True, "count": len(results), "results": results})


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
