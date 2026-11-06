#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ICN CONNECT 데이터 갱신 스크립트

공공데이터포털 "인천국제공항공사_항공기 운항 현황 상세 조회" API를 호출해서
Terminal 2 의 도착/출발 항공편을 정리하고, 화면(index.html)이 읽는 data.js 를 만듭니다.
작업 스케줄러(Windows) 또는 cron(리눅스)에서 5분 또는 10분마다 실행하는 용도입니다.

사용법
  python icn_gate_sync.py --probe   API 응답 필드를 확인합니다. (호출 2회, 파일을 만들지 않음)
  python icn_gate_sync.py --demo    API 없이 샘플 data.js 를 만듭니다. (화면과 폴더 권한 점검용)
  python icn_gate_sync.py           정식 실행: API 호출 후 data.js 갱신

필요 환경: Python 3.8 이상 (추가 설치 없음)
설정: 이 파일과 같은 폴더의 config.ini
"""
import argparse
import configparser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("ICN_STATE_PATH") or os.path.join(HERE, "sync_state.json")
LOG_PATH = os.path.join(HERE, "sync.log")

# ---------------------------------------------------------------------------
# 응답 필드 이름 매핑
# API 응답의 영문 필드 이름은 --probe 결과로 확인한 뒤 아래 목록을 맞춥니다.
# 앞쪽 이름부터 찾고, 없으면 contains 에 적은 글자가 들어간 필드를 찾습니다.
# ---------------------------------------------------------------------------
FIELDS = {
    "flight":   (["flightId", "flightid", "flightNo"], ()),
    "gate":     (["gateNumber", "gatenumber", "gate"], ()),
    "stand":    (["fstandPosition", "standPosition", "stand"], ()),
    "terminal": (["terminalId", "terminalid", "terminal"], ()),
    "apt_code": (["airportCode", "airportcode"], ()),
    "apt_name": (["airport", "airportName"], ()),
    "sched":    (["scheduleDatetime", "scheduleDateTime", "scheduledatetime"], ("schedule",)),
    "est":      (["estimatedDatetime", "estimatedDateTime", "estimateddatetime"], ("estimated", "changed")),
    "actual":   (["actualDatetime", "actualDateTime"], ("actual",)),
    "reg":      (["acReg", "registrationNo", "regNo", "aircraftRegNo", "acRegNo", "aircraftRegistration"], ("reg",)),
    "type":     (["acType", "aircraftSubtype", "aircraftSubType", "acSubType", "subtype"], ("subtype", "actype", "aircrafttype")),
    "remark":   (["remark", "status"], ("remark",)),
    "master":   (["masterflightid", "masterFlightId"], ("master",)),
}


class ApiError(Exception):
    pass


class BudgetExceeded(Exception):
    pass


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1000000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config(path):
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path, encoding="utf-8"):
        sys.exit("설정 파일을 찾을 수 없습니다: %s" % path)
    return cp


def mask(text, key):
    if not key:
        return text
    for k in {key, urllib.parse.quote(key, safe="")}:
        text = text.replace(k, "***")
    return text


# ---------------------------------------------------------------------------
# API 호출
# ---------------------------------------------------------------------------
class Budget:
    """하루 호출 횟수를 세어서 한도를 넘지 않게 합니다."""

    def __init__(self, state, limit):
        today = datetime.now().strftime("%Y%m%d")
        if state.get("date") != today:
            state["date"] = today
            state["calls"] = 0
        self.state = state
        self.limit = limit

    def use(self):
        if self.state["calls"] >= self.limit:
            raise BudgetExceeded("오늘 호출 한도(%d회)에 도달했습니다." % self.limit)
        self.state["calls"] += 1

    @property
    def calls(self):
        return self.state["calls"]


def call_api(cfg, op, params):
    base = cfg.get("api", "base_url").rstrip("/")
    # 인증키는 환경변수 ICN_SERVICE_KEY 가 있으면 그것을 우선 사용합니다. (GitHub Actions 의 Secrets 용)
    key = urllib.parse.unquote((os.environ.get("ICN_SERVICE_KEY") or cfg.get("api", "service_key", fallback="")).strip())
    if not key or "인증키" in key:
        raise ApiError("인증키가 비어 있습니다. config.ini 의 service_key 또는 환경변수 ICN_SERVICE_KEY 를 확인해 주세요.")
    q = dict(serviceKey=key, type="json")
    q.update(params)
    url = "%s/%s?%s" % (base, op, urllib.parse.urlencode(q, quote_via=urllib.parse.quote))
    req = urllib.request.Request(url, headers={"User-Agent": "icn-connect-sync/1.0", "Accept": "application/json"})
    timeout = cfg.getint("api", "timeout_sec", fallback=20)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        raise ApiError("HTTP %s %s" % (e.code, mask(body, key)))
    except urllib.error.URLError as e:
        raise ApiError("접속 실패: %s" % mask(str(e.reason), key))
    try:
        data = json.loads(text)
    except ValueError:
        raise ApiError("JSON이 아닌 응답입니다: %s" % mask(text[:300].replace("\n", " "), key))
    header = (data.get("response") or {}).get("header") or {}
    code = str(header.get("resultCode", ""))
    if code and code not in ("00", "0"):
        raise ApiError("API 오류 %s: %s" % (code, header.get("resultMsg", "")))
    return data


def extract_items(data):
    body = (data.get("response") or {}).get("body") or {}
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    try:
        total = int(body.get("totalCount") or len(items))
    except (TypeError, ValueError):
        total = len(items)
    return items, total


def fetch_day(cfg, budget, op, date_str):
    rows, page = [], 1
    per = cfg.getint("api", "rows_per_page", fallback=1000)
    while True:
        budget.use()
        params = {
            "pageNo": page, "numOfRows": per,
            "searchdtCode": cfg.get("api", "searchdt_code", fallback="E"),
            "searchDate": date_str, "searchFrom": "0000", "searchTo": "2400",
        }
        poc = cfg.get("api", "passenger_or_cargo", fallback="P").strip()
        if poc:                       # 제2터미널 여객기 전용 API 는 이 값이 필요 없으므로 비워 둡니다.
            params["passengerOrCargo"] = poc
        data = call_api(cfg, op, params)
        items, total = extract_items(data)
        rows.extend(items)
        if not items or len(rows) >= total or page >= 20:
            break
        page += 1
    return rows


# ---------------------------------------------------------------------------
# 정리(정규화)
# ---------------------------------------------------------------------------
def pick(item, name):
    names, contains = FIELDS[name]
    low = dict((k.lower(), v) for k, v in item.items())
    for n in names:
        v = low.get(n.lower())
        if v not in (None, ""):
            return str(v).strip()
    for k, v in low.items():
        if v not in (None, "") and any(c in k for c in contains):
            return str(v).strip()
    return ""


def parse_dt(s):
    """'202611051435' 또는 '20261105143500' 형태를 ISO 문자열로. 실패하면 None."""
    digits = re.sub(r"\D", "", s or "")
    if len(digits) < 12:
        return None
    try:
        return datetime.strptime(digits[:12], "%Y%m%d%H%M").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def normalize(items, direction, cfg):
    tvals = set(v.strip().upper() for v in cfg.get("filter", "terminal_values", fallback="P03,T2").split(",") if v.strip())
    arr_done = set(v.strip() for v in cfg.get("filter", "arrived_remarks", fallback="도착,착륙").split(",") if v.strip())
    dep_done = set(v.strip() for v in cfg.get("filter", "departed_remarks", fallback="출발").split(",") if v.strip())
    done_remarks = arr_done if direction == "ARR" else dep_done
    out = {}
    for it in items:
        if tvals and pick(it, "terminal").upper() not in tvals:
            continue
        gate = re.sub(r"\D", "", pick(it, "gate")) or re.sub(r"\D", "", pick(it, "stand"))   # 게이트가 비어 있으면 주기장 값 사용, 236R 같은 값은 숫자만 사용
        sched = parse_dt(pick(it, "sched"))
        if not sched:
            continue                                   # 게이트가 없는 항공편도 '미배정' 줄에 보여주기 위해 남겨 둡니다.
        est = parse_dt(pick(it, "est"))
        actual = parse_dt(pick(it, "actual"))
        if not actual and pick(it, "remark") in done_remarks:
            actual = est or sched                         # 명시적 실제 시각이 없으면 '출발/도착' 상태일 때 변경 시각을 실제로 봄
        flight = pick(it, "flight")
        master = pick(it, "master")
        rec = {
            "dir": direction, "flight": flight, "reg": pick(it, "reg"), "type": pick(it, "type"),
            "gate": gate, "apt": pick(it, "apt_code"), "aptName": pick(it, "apt_name"),
            "sched": sched, "est": est if est and est != sched else None, "actual": actual,
        }
        is_master = (not master) or master == flight
        key = (direction, rec["reg"] or "%s|%s" % (gate, rec["apt"]), sched)   # 코드셰어 중복 제거
        prev = out.get(key)
        if prev is None or (is_master and not prev[1]):
            out[key] = (rec, is_master)
    return [v[0] for v in out.values()]


# ---------------------------------------------------------------------------
# 상태 파일, 출력
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError as e:
        log("상태 파일을 저장하지 못했습니다: %s" % e)


def out_path(cfg):
    return os.environ.get("ICN_DATA_JS_PATH") or cfg.get("output", "data_js_path")


def write_data_js(path, payload):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    text = "window.ICN_DATA = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)        # 화면이 쓰다 만 파일을 읽지 않도록 한 번에 교체


# ---------------------------------------------------------------------------
# 실행 모드
# ---------------------------------------------------------------------------
def run_sync(cfg):
    state = load_state()
    budget = Budget(state, cfg.getint("limits", "daily_call_limit", fallback=450))
    back = cfg.getint("filter", "days_back", fallback=1)
    ahead = cfg.getint("filter", "days_ahead", fallback=1)
    refresh_min = cfg.getint("limits", "neighbor_refresh_min", fallback=60)
    ops = (("ARR", cfg.get("api", "arrivals_op")), ("DEP", cfg.get("api", "departures_op")))
    now = datetime.now()
    today = now.date()
    dates = [today + timedelta(days=i) for i in range(-back, ahead + 1)]
    cache = state.setdefault("days", {})
    for k in list(cache):
        if k not in [d.strftime("%Y%m%d") for d in dates]:
            del cache[k]

    flights = []
    for d in dates:
        ds = d.strftime("%Y%m%d")
        entry = cache.get(ds)
        age_min = None
        if entry:
            age_min = (now - datetime.strptime(entry["at"], "%Y-%m-%dT%H:%M:%S")).total_seconds() / 60
        need = (d == today) or entry is None or age_min >= refresh_min
        if need:
            try:
                day = []
                for direction, op in ops:
                    day += normalize(fetch_day(cfg, budget, op, ds), direction, cfg)
                cache[ds] = {"at": now.strftime("%Y-%m-%dT%H:%M:%S"), "flights": day}
                entry = cache[ds]
            except (ApiError, BudgetExceeded) as e:
                if d == today:
                    save_state(state)
                    raise
                log("%s 데이터 갱신 실패(이전 값 사용): %s" % (ds, e))
        if entry:
            flights += entry["flights"]

    # 조회 날짜 경계에서 같은 항공편이 두 번 들어오는 경우를 제거
    uniq = {}
    for f in flights:
        uniq[(f["dir"], f["flight"], f["sched"], f["gate"])] = f
    flights = sorted(uniq.values(), key=lambda f: (f["sched"], f["dir"]))
    payload = {
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M"),
        "intervalMin": cfg.getint("schedule", "interval_min", fallback=5),
        "range": {"from": dates[0].isoformat(), "to": dates[-1].isoformat()},
        "terminal": "T2",
        "flights": flights,
    }
    write_data_js(out_path(cfg), payload)
    save_state(state)
    no_reg = sum(1 for f in flights if not f["reg"])
    no_gate = sum(1 for f in flights if not f["gate"])
    log("완료: T2 항공편 %d건 (등록기호 없음 %d건, 게이트 미배정 %d건), 오늘 호출 %d/%d회" % (len(flights), no_reg, no_gate, budget.calls, budget.limit))
    if flights and no_reg == len(flights):
        log("경고: 등록기호가 모두 비어 있습니다. --probe 로 필드 이름을 확인해 주세요.")
    if not flights:
        log("경고: T2 항공편이 0건입니다. terminal_values 와 응답 필드를 --probe 로 확인해 주세요.")


def run_probe(cfg):
    state = {}
    budget = Budget(state, 10)
    today = datetime.now().strftime("%Y%m%d")
    for label, key in (("도착", "arrivals_op"), ("출발", "departures_op")):
        op = cfg.get("api", key)
        print("\n===== %s (%s) =====" % (label, op))
        budget.use()
        params = {"pageNo": 1, "numOfRows": 1000, "searchDate": today, "searchFrom": "0000", "searchTo": "2400"}
        poc = cfg.get("api", "passenger_or_cargo", fallback="P").strip()
        if poc:
            params["passengerOrCargo"] = poc
        data = call_api(cfg, op, params)
        items, total = extract_items(data)
        print("전체 건수(totalCount):", total, " / 이번에 받은 건수:", len(items))
        with open(os.path.join(HERE, "probe_%s.json" % ("arrivals" if key.startswith("arr") else "departures")), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if not items:
            continue
        print("응답 필드:", ", ".join(items[0].keys()))
        print("첫 항목 예시:")
        print(json.dumps(items[0], ensure_ascii=False, indent=2))
        print("터미널 값 분포:", dict(Counter(pick(i, "terminal") for i in items)))
        print("필드 매핑 결과(받은 %d건 중 값이 있는 건수):" % len(items))
        for name in ("flight", "gate", "terminal", "apt_code", "sched", "est", "actual", "reg", "type", "remark"):
            print("  %-9s %3d건  예: %s" % (name, sum(1 for i in items if pick(i, name)), next((pick(i, name) for i in items if pick(i, name)), "-")))
        print("현황(remark) 값 분포:", dict(Counter(pick(i, "remark") for i in items)))
        print("기종 코드 분포:", dict(Counter(pick(i, "type") for i in items).most_common(30)))
        gates = sorted(set(pick(i, "gate") for i in items if pick(i, "gate")))
        stands = sorted(set(pick(i, "stand") for i in items if pick(i, "stand")))
        print("게이트번호 값 (%d종): %s" % (len(gates), ", ".join(gates)))
        print("주기장 값 (%d종): %s" % (len(stands), ", ".join(stands)))
        diff = [(pick(i, "flight"), pick(i, "gate"), pick(i, "stand")) for i in items if pick(i, "gate") != pick(i, "stand")]
        print("게이트와 주기장이 다른 항목: %d건, 예: %s" % (len(diff), diff[:6]))
        print("게이트 없음: %d건 / 등록기호 없음: %d건" % (sum(1 for i in items if not pick(i, "gate")), sum(1 for i in items if not pick(i, "reg"))))
        now = datetime.now()

        def mins(i):
            t = parse_dt(pick(i, "est") or pick(i, "sched"))
            return None if not t else (datetime.strptime(t, "%Y-%m-%dT%H:%M") - now).total_seconds() / 60

        print("지금(%s) 기준 앞으로 남은 항공편의 게이트/주기장 배정 현황:" % now.strftime("%H:%M"))
        for lo, hi, label in ((0, 120, "0~2시간 뒤"), (120, 240, "2~4시간 뒤"), (240, 480, "4~8시간 뒤"), (480, 100000, "8시간 이후")):
            sel = [i for i in items if mins(i) is not None and lo <= mins(i) < hi]
            print("  %-10s 전체 %3d건 / 게이트 있음 %3d / 주기장 있음 %3d" % (label, len(sel), sum(1 for i in sel if pick(i, "gate")), sum(1 for i in sel if pick(i, "stand"))))
        budget.use()
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
        items2, total2 = extract_items(call_api(cfg, op, dict(params, searchDate=tomorrow)))
        print("내일(%s): 전체 %d건 / 게이트 있음 %d / 주기장 있음 %d" % (tomorrow, len(items2), sum(1 for i in items2 if pick(i, "gate")), sum(1 for i in items2 if pick(i, "stand"))))
    print("\n저장된 파일: probe_arrivals.json, probe_departures.json (인증키는 들어 있지 않습니다)")


def run_demo(cfg):
    now = datetime.now().replace(second=0, microsecond=0)
    base = now - timedelta(minutes=now.minute % 5)
    cities = [("NRT", "도쿄 나리타"), ("KIX", "오사카 간사이"), ("BKK", "방콕"), ("SIN", "싱가포르"), ("ATL", "애틀랜타"), ("HKG", "홍콩")]
    flights = []
    for i, gate in enumerate(range(231, 243)):
        arr = base + timedelta(minutes=-170 + i * 22)
        dep = arr + timedelta(minutes=75 + (i % 4) * 25)
        code, name = cities[i % len(cities)]
        reg, typ = "HL7%03d" % (500 + i), ("A350-900" if i % 2 == 0 else "B787-9")
        fmt = lambda t: t.strftime("%Y-%m-%dT%H:%M")
        a_est = arr + timedelta(minutes=20) if i % 5 == 0 else None
        d_est = dep + timedelta(minutes=20) if i % 5 == 0 else None
        a_eff, d_eff = (a_est or arr), (d_est or dep)
        flights.append({"dir": "ARR", "flight": "KE%d" % (200 + i), "reg": reg, "type": typ, "gate": str(gate), "apt": code, "aptName": name,
                        "sched": fmt(arr), "est": fmt(a_est) if a_est else None, "actual": fmt(a_eff) if a_eff <= now else None})
        flights.append({"dir": "DEP", "flight": "KE%d" % (201 + i), "reg": reg, "type": typ, "gate": str(gate), "apt": code, "aptName": name,
                        "sched": fmt(dep), "est": fmt(d_est) if d_est else None, "actual": fmt(d_eff) if d_eff <= now else None})
    today = now.date()
    payload = {
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M"),
        "intervalMin": cfg.getint("schedule", "interval_min", fallback=5),
        "range": {"from": (today - timedelta(days=1)).isoformat(), "to": (today + timedelta(days=1)).isoformat()},
        "terminal": "T2", "flights": flights,
    }
    write_data_js(out_path(cfg), payload)
    log("샘플 data.js 를 만들었습니다: %s (항공편 %d건)" % (out_path(cfg), len(flights)))


def main():
    ap = argparse.ArgumentParser(description="ICN CONNECT 데이터 갱신")
    ap.add_argument("--probe", action="store_true", help="API 응답 필드 확인")
    ap.add_argument("--demo", action="store_true", help="샘플 data.js 생성")
    ap.add_argument("--config", default=os.path.join(HERE, "config.ini"), help="설정 파일 경로")
    args = ap.parse_args()
    cfg = load_config(args.config)
    tzname = cfg.get("schedule", "timezone", fallback="").strip()      # 서버가 UTC 인 경우(GitHub 등) 한국 시각으로 맞춤
    if tzname and hasattr(time, "tzset"):
        os.environ["TZ"] = tzname
        time.tzset()
    try:
        if args.probe:
            run_probe(cfg)
        elif args.demo:
            run_demo(cfg)
        else:
            run_sync(cfg)
    except (ApiError, BudgetExceeded) as e:
        log("실패: %s" % e)
        sys.exit(1)
    except OSError as e:
        log("파일 오류: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
