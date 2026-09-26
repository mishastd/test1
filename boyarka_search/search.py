#!/usr/bin/env python3
"""Поиск домов вокруг Боярки (Киевская обл.) через DIM.RIA API + OpenStreetMap.

Критерии по умолчанию:
  * дом (продажа) в радиусе 30 км от Боярки, цена <= 35 000 USD;
  * больница/поликлиника, аптека и магазин — не дальше 5 км;
  * в радиусе 10 км нет заводов, складов, промзон, предприятий и военных объектов.

Только стандартная библиотека Python 3.8+. Запуск:
  python3 search.py             # пробный запуск: только OSM, 0 запросов к RIA
  export RIA_API_KEY=...        # ключ developers.ria.com
  python3 search.py --go        # реальный поиск, не больше --max-requests запросов
Результат: results.csv, results.html (карта), cache/ (ответы API).
"""
import argparse
import csv
import html
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

BOYARKA = (50.3290, 30.2890)
RIA_BASE = "https://developers.ria.com/dom"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")

# ---------- геометрия ----------

def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _to_xy(p, origin):
    # локальная равнопромежуточная проекция, км — достаточно точно на десятках км
    kx = 111.32 * math.cos(math.radians(origin[0]))
    return ((p[1] - origin[1]) * kx, (p[0] - origin[0]) * 110.574)


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _inside(p, ring):
    x, y = p
    res = False
    for i in range(len(ring)):
        (x1, y1), (x2, y2) = ring[i - 1], ring[i]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            res = not res
    return res


def dist_to_feature_km(pt, feat):
    """Расстояние от точки до OSM-объекта (точка, линия или полигон)."""
    if feat["rings"]:
        p = (0.0, 0.0)
        best = float("inf")
        for ring in feat["rings"]:
            xy = [_to_xy(q, pt) for q in ring]
            if len(xy) >= 3 and _inside(p, xy):
                return 0.0
            for i in range(1, len(xy)):
                best = min(best, _seg_dist(p, xy[i - 1], xy[i]))
            if len(xy) == 1:
                best = min(best, math.hypot(*xy[0]))
        return best
    return haversine_km(pt, feat["center"])

# ---------- HTTP ----------

def http_get(url, data=None, timeout=180):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "boyarka-house-search/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def cached_json(name, fetch):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    obj = json.loads(fetch())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return obj

# ---------- DIM.RIA (лимит запросов!) ----------

class Budget:
    """Счётчик реальных запросов к DIM.RIA, хранится в cache/ria_budget.json между запусками.
    Ответы кешируются навсегда, поэтому один и тот же запрос никогда не оплачивается дважды."""

    def __init__(self, limit):
        self.path = os.path.join(CACHE, "ria_budget.json")
        self.limit = limit
        self.total = limit
        self.used = 0
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.used = json.load(f).get("used", 0)

    def left(self):
        return max(0, self.limit - self.used)

    def spend(self):
        if self.used >= self.limit:
            raise BudgetExceeded()
        self.used += 1
        os.makedirs(CACHE, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"used": self.used, "limit": self.total}, f)


class BudgetExceeded(Exception):
    pass


def ria_get(key, budget, name, path, params, delay):
    def fetch():
        budget.spend()
        time.sleep(delay)
        url = "%s/%s?%s" % (RIA_BASE, path, urllib.parse.urlencode([("api_key", key)] + params))
        print("  [RIA %d/%d] %s" % (budget.used, budget.total, path), file=sys.stderr)
        return http_get(url)
    return cached_json(name, fetch)


def ria_cities(key, budget, args):
    """Список населённых пунктов области: 1 запрос."""
    res = ria_get(key, budget, "cities_%d.json" % args.state_id, "cities/%d" % args.state_id,
                  [("lang_id", 4)], args.delay)
    out = {}
    for c in res if isinstance(res, list) else res.get("items", []):
        cid = c.get("cityID") or c.get("city_id") or c.get("value") or c.get("id")
        for k in ("name", "name_uk", "name_ru", "nameUk", "nameRu"):
            if c.get(k) and cid:
                out.setdefault(norm_name(c[k]), int(cid))
    return out


def ria_search_ids(key, budget, args, city_id):
    """id объявлений (дома, продажа, цена <= max) в одном населённом пункте. 1 запрос на страницу."""
    ids, page = [], 0
    while True:
        params = [
            ("category", args.category), ("operation_type", 1), ("state_id", args.state_id),
            ("city_id", city_id), ("page", page),
            # цена до N в USD (как в фильтрах dim.ria.com); дополнительно проверяется локально
            ("characteristic[234][to]", args.max_price), ("characteristic[242]", 239),
        ] + [("realty_type", t) for t in args.realty_types]
        res = ria_get(key, budget, "search_c%d_p%d.json" % (city_id, page), "search", params, args.delay)
        items = res.get("items") or []
        ids.extend(items)
        if not items or len(ids) >= int(res.get("count") or 0) or page + 1 >= args.max_pages:
            return list(dict.fromkeys(ids)), int(res.get("count") or len(ids))
        page += 1


def ria_info(key, budget, rid, args):
    return ria_get(key, budget, "info_%s.json" % rid, "info/%s" % rid, [("lang_id", 4)], args.delay)


def norm_name(s):
    s = s.lower().replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("ё", "е").strip()
    for p in ("смт ", "с. ", "м. ", "с.", "м.", "село ", "селище "):
        if s.startswith(p):
            s = s[len(p):].strip()
    return s.split(" (")[0]


def _num(v):
    try:
        return float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_listing(info):
    price = _num((info.get("priceArr") or {}).get("1"))
    if price is None and str(info.get("currency_type", "")).strip() in ("$", "USD", "1"):
        price = _num(info.get("price"))
    lat, lon = _num(info.get("latitude")), _num(info.get("longitude"))
    url = info.get("beautiful_url") or ""
    return {
        "id": info.get("realty_id"),
        "price_usd": price,
        "lat": lat, "lon": lon,
        "city": info.get("city_name") or "",
        "street": info.get("street_name") or "",
        "area_m2": info.get("total_square_meters") or "",
        "land": info.get("plot_area") or info.get("land_square") or "",
        "url": ("https://dim.ria.com/uk/" + url.lstrip("/")) if url else "https://dim.ria.com/realty-%s.html" % info.get("realty_id"),
        "desc": (info.get("description_uk") or info.get("description") or "")[:300],
    }

# ---------- OpenStreetMap ----------

OVERPASS_QUERY = """
[out:json][timeout:300];
(
  nwr["amenity"~"^(hospital|clinic|doctors)$"]({bbox});
  nwr["healthcare"~"^(hospital|clinic|centre|doctor)$"]({bbox});
  nwr["amenity"="pharmacy"]({bbox});
  nwr["healthcare"="pharmacy"]({bbox});
  nwr["shop"~"^(supermarket|convenience|general|grocery|mall|department_store)$"]({bbox});
  nwr["landuse"~"^(industrial|military)$"]({bbox});
  nwr["military"]({bbox});
  nwr["man_made"="works"]({bbox});
  nwr["industrial"]({bbox});
  way["building"~"^(industrial|warehouse|factory)$"]({bbox});
  node["place"~"^(city|town|village|hamlet)$"]({bbox});
);
out geom;
"""


def classify(tags):
    if tags.get("place"):
        return "place"
    a, hc, shop = tags.get("amenity"), tags.get("healthcare"), tags.get("shop")
    if a in ("hospital", "clinic", "doctors") or hc in ("hospital", "clinic", "centre", "doctor"):
        return "medical"
    if a == "pharmacy" or hc == "pharmacy":
        return "pharmacy"
    if shop:
        return "shop"
    if tags.get("landuse") == "military" or "military" in tags:
        return "military"
    if tags.get("building") == "warehouse" or tags.get("industrial") in ("warehouse", "depot", "logistics"):
        return "warehouse"
    return "industrial"


def osm_features(radius_km):
    dlat = radius_km / 110.574
    dlon = radius_km / (111.32 * math.cos(math.radians(BOYARKA[0])))
    bbox = "%.5f,%.5f,%.5f,%.5f" % (BOYARKA[0] - dlat, BOYARKA[1] - dlon, BOYARKA[0] + dlat, BOYARKA[1] + dlon)
    q = OVERPASS_QUERY.format(bbox=bbox).encode()

    def fetch():
        last = None
        for u in OVERPASS_URLS:
            try:
                return http_get(u, data=urllib.parse.urlencode({"data": q}).encode(), timeout=360)
            except Exception as e:  # пробуем зеркало
                last = e
                print("  Overpass %s: %s" % (u, e), file=sys.stderr)
        raise last

    data = cached_json("osm_%dkm.json" % radius_km, fetch)
    feats = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        rings = []
        if el["type"] == "way" and el.get("geometry"):
            rings = [[(g["lat"], g["lon"]) for g in el["geometry"]]]
        elif el["type"] == "relation":
            rings = [[(g["lat"], g["lon"]) for g in m["geometry"]]
                     for m in el.get("members", []) if m.get("geometry") and m.get("role") != "inner"]
        if el["type"] == "node":
            center = (el["lat"], el["lon"])
        elif rings and rings[0]:
            pts = [p for r in rings for p in r]
            center = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        else:
            continue
        feats.append({"kind": classify(tags), "name": tags.get("name") or tags.get("operator") or "",
                      "tags": tags, "center": center, "rings": rings, "osm": "%s/%s" % (el["type"], el["id"])})
    return feats


def nearest(pt, feats, max_km):
    best = None
    for f in feats:
        if haversine_km(pt, f["center"]) > max_km + 5:  # грубый отсев
            continue
        d = dist_to_feature_km(pt, f)
        if d <= max_km and (best is None or d < best[0]):
            best = (d, f)
    return best

# ---------- отчёт ----------

def label(f):
    t = f["tags"]
    kind = t.get("amenity") or t.get("healthcare") or t.get("shop") or t.get("landuse") or t.get("military") \
        or t.get("industrial") or t.get("man_made") or t.get("building") or f["kind"]
    return "%s %s (%s)" % (kind, f["name"], f["osm"]) if f["name"] else "%s (%s)" % (kind, f["osm"])


def write_html(path, rows):
    markers = [{"lat": r["lat"], "lon": r["lon"], "ok": r["ok"],
                "t": "<b>%s $</b> %s %s<br>%s<br><a href='%s' target=_blank>dim.ria</a>" % (
                    int(r["price_usd"]), html.escape(r["city"]), html.escape(r["street"]),
                    html.escape(r["verdict"]), html.escape(r["url"]))} for r in rows]
    page = """<!doctype html><meta charset=utf-8><title>Дома у Боярки</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel=stylesheet href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#m{height:100%%;margin:0}</style><div id=m></div><script>
var m=L.map('m').setView([%f,%f],10);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; OpenStreetMap'}).addTo(m);
L.circle([%f,%f],{radius:30000,fill:false}).addTo(m);
%s.forEach(function(p){L.circleMarker([p.lat,p.lon],{radius:7,color:p.ok?'#1a7f37':'#b35900'}).bindPopup(p.t).addTo(m)});
</script>""" % (BOYARKA[0], BOYARKA[1], BOYARKA[0], BOYARKA[1], json.dumps(markers, ensure_ascii=False))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def check_point(pt, by, bad, args, hazard_km=None):
    res, problems = {}, []
    for k, name in (("medical", "больница"), ("pharmacy", "аптека"), ("shop", "магазин")):
        res[k] = nearest(pt, by[k], args.amenity_km)
        if not res[k]:
            problems.append("нет: %s ≤%g км" % (name, args.amenity_km))
    hazard = nearest(pt, bad, args.clear_km if hazard_km is None else hazard_km)
    if hazard:
        problems.append("рядом %s: %s — %.1f км" % (hazard[1]["kind"], label(hazard[1]), hazard[0]))
    return res, hazard, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--radius", type=float, default=30, help="радиус поиска от Боярки, км (30)")
    ap.add_argument("--max-price", type=int, default=35000, help="максимальная цена, USD (35000)")
    ap.add_argument("--amenity-km", type=float, default=5, help="больница/аптека/магазин не дальше, км (5)")
    ap.add_argument("--clear-km", type=float, default=10, help="нет промзон/складов/военных в радиусе, км (10)")
    ap.add_argument("--min-industrial-ha", type=float, default=0,
                    help="игнорировать промзоны меньше N га (0 = учитывать все)")
    ap.add_argument("--village-slack-km", type=float, default=2,
                    help="допуск при оценке сёл по центру: дом может стоять в N км от центра (2)")
    ap.add_argument("--budget", type=int, default=100, help="ВСЕГО запросов к DIM.RIA за все запуски (100)")
    ap.add_argument("--max-requests", type=int, default=40,
                    help="максимум запросов к DIM.RIA за этот запуск (40)")
    ap.add_argument("--max-pages", type=int, default=1, help="страниц поиска на населённый пункт (1)")
    ap.add_argument("--max-places", type=int, default=15, help="сколько лучших сёл/городов опрашивать (15)")
    ap.add_argument("--go", action="store_true",
                    help="реально тратить запросы к DIM.RIA (без флага — только план по OSM, 0 запросов)")
    ap.add_argument("--category", type=int, default=4, help="категория DIM.RIA (4 = дома)")
    ap.add_argument("--realty-types", type=int, nargs="+", default=[5, 6],
                    help="типы DIM.RIA (5 = дом, 6 = часть дома, 7 = дача)")
    ap.add_argument("--state-id", type=int, default=10, help="область DIM.RIA (10 = Киевская)")
    ap.add_argument("--delay", type=float, default=1.0, help="пауза между запросами к API, сек")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args()

    # 1. OpenStreetMap — бесплатно, делаем первым, чтобы не тратить запросы RIA впустую
    print("1/4 OpenStreetMap: инфраструктура, промзоны, населённые пункты...", file=sys.stderr)
    feats = osm_features(int(math.ceil(args.radius + args.clear_km + 1)))
    by = {k: [f for f in feats if f["kind"] == k] for k in ("medical", "pharmacy", "shop")}
    bad = [f for f in feats if f["kind"] in ("industrial", "warehouse", "military")]
    if args.min_industrial_ha > 0:
        def area_ha(f):
            if not f["rings"]:
                return 0
            xy = [_to_xy(p, f["center"]) for p in f["rings"][0]]
            return abs(sum(xy[i - 1][0] * xy[i][1] - xy[i][0] * xy[i - 1][1] for i in range(len(xy)))) / 2 * 100
        bad = [f for f in bad if f["kind"] == "military" or area_ha(f) >= args.min_industrial_ha]
    print("  медицина %d, аптеки %d, магазины %d, пром/склад/военные %d" % (
        len(by["medical"]), len(by["pharmacy"]), len(by["shop"]), len(bad)), file=sys.stderr)

    # 2. Оценка населённых пунктов по центру (с допуском) — какие вообще имеет смысл искать
    places = []
    for f in feats:
        if f["kind"] != "place" or not f["name"]:
            continue
        d = haversine_km(BOYARKA, f["center"])
        if d > args.radius:
            continue
        _, hazard, problems = check_point(f["center"], by, bad, args,
                                          hazard_km=max(0.5, args.clear_km - args.village_slack_km))
        # ближайшее «вредное» — для сортировки: чем дальше, тем лучше
        hz = nearest(f["center"], bad, args.clear_km + 20)
        places.append({"name": f["name"], "names": [f["tags"].get(k) for k in ("name", "name:uk", "name:ru") if f["tags"].get(k)],
                       "dist": d, "ok": not problems, "problems": problems, "hazard_km": hz[0] if hz else 99,
                       "center": f["center"]})
    good = sorted([p for p in places if p["ok"]], key=lambda p: -p["hazard_km"])
    print("2/4 Населённых пунктов в радиусе %g км: %d, проходят критерии: %d" % (args.radius, len(places), len(good)),
          file=sys.stderr)
    for p in good[:args.max_places]:
        print("   + %-25s %4.1f км от Боярки, до промзоны %.1f км" % (p["name"], p["dist"], p["hazard_km"]), file=sys.stderr)
    if not good:
        near = sorted(places, key=lambda p: -p["hazard_km"])[:10]
        print("  Ни один пункт не проходит. Лучшие по удалённости от промзон:", file=sys.stderr)
        for p in near:
            print("   - %-25s до промзоны %.1f км; %s" % (p["name"], p["hazard_km"], "; ".join(p["problems"])), file=sys.stderr)
        print("  Ослабьте критерии (--clear-km 5 или --min-industrial-ha 5). Запросы RIA не тратились.", file=sys.stderr)
        return

    targets = good[:args.max_places]
    budget = Budget(args.budget)
    need = 1 + len(targets) * args.max_pages
    print("  План: 1 запрос (список пунктов) + до %d поисков + подробности по найденным домам." % (need - 1),
          file=sys.stderr)
    print("  Бюджет RIA: использовано %d из %d, на этот запуск разрешено %d." % (
        budget.used, budget.limit, min(budget.left(), args.max_requests)), file=sys.stderr)
    if not args.go:
        print("\nЭто пробный запуск (0 запросов к DIM.RIA). Для реального поиска добавьте --go.")
        return
    key = os.environ.get("RIA_API_KEY")
    if not key:
        sys.exit("Задайте ключ: export RIA_API_KEY=...")
    run_limit = budget.used + min(budget.left(), args.max_requests)
    budget.limit = min(budget.limit, run_limit)

    listings = []
    try:
        # 3. Поиск только в подходящих населённых пунктах
        print("3/4 DIM.RIA: поиск в подходящих пунктах...", file=sys.stderr)
        cities = ria_cities(key, budget, args)
        all_ids = []
        for p in targets:
            cid = next((cities[norm_name(n)] for n in p["names"] if norm_name(n) in cities), None)
            if cid is None:
                print("   ? %s — нет в справочнике RIA, пропуск" % p["name"], file=sys.stderr)
                continue
            ids, total = ria_search_ids(key, budget, args, cid)
            print("   %s: объявлений %d" % (p["name"], total), file=sys.stderr)
            all_ids.extend(ids)
        all_ids = list(dict.fromkeys(all_ids))
        # 4. Подробности (координаты, цена) — сколько позволяет бюджет
        print("4/4 DIM.RIA: подробности по %d объявлениям (осталось запросов: %d)..." % (
            len(all_ids), budget.left()), file=sys.stderr)
        for rid in all_ids:
            l = parse_listing(ria_info(key, budget, rid, args))
            if l["price_usd"] is None or l["price_usd"] > args.max_price or not l["lat"] or not l["lon"]:
                continue
            l["dist_boyarka_km"] = haversine_km(BOYARKA, (l["lat"], l["lon"]))
            if l["dist_boyarka_km"] <= args.radius:
                listings.append(l)
    except BudgetExceeded:
        print("  ! Лимит запросов этого запуска исчерпан — остальное можно дособрать следующим запуском"
              " (кеш сохранён, повторно запросы не тратятся).", file=sys.stderr)

    rows = []
    for l in listings:
        res, hazard, problems = check_point((l["lat"], l["lon"]), by, bad, args)
        fmt = lambda n: "%.1f км — %s" % (n[0], label(n[1])) if n else ""
        rows.append(dict(l, ok=not problems, verdict="OK" if not problems else "; ".join(problems),
                         medical=fmt(res["medical"]), pharmacy=fmt(res["pharmacy"]), shop=fmt(res["shop"]),
                         nearest_hazard_km=round(hazard[0], 2) if hazard else ">%g" % args.clear_km))
    rows.sort(key=lambda r: (not r["ok"], r["price_usd"]))

    cols = ["ok", "price_usd", "dist_boyarka_km", "city", "street", "area_m2", "land", "medical", "pharmacy",
            "shop", "nearest_hazard_km", "verdict", "url", "lat", "lon", "id", "desc"]
    with open(args.out + ".csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(dict(r, dist_boyarka_km=round(r["dist_boyarka_km"], 1)))
    write_html(args.out + ".html", rows)

    ok = [r for r in rows if r["ok"]]
    print("\nПодходят все критерии: %d из %d. Запросов RIA израсходовано всего: %d из %d." % (
        len(ok), len(rows), budget.used, args.budget))
    for r in ok[:30]:
        print("  $%-6d %5.1f км  %s %s\n          %s" % (r["price_usd"], r["dist_boyarka_km"], r["city"], r["street"], r["url"]))
    print("\nФайлы: %s.csv, %s.html" % (args.out, args.out))


if __name__ == "__main__":
    main()
