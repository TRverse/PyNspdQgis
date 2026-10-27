import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from pynspd import Nspd
from shapely.geometry import mapping
from tqdm import tqdm

# ==================== НАСТРОЙКИ ====================
CENTER_X = 3726270.23      # центр квадрата (EPSG:3857)
CENTER_Y = 5548983.26
SIZE_METERS = 7000         # сторона квадрата, м
# шаг сетки, м (меньше шаг -> меньше пропусков, но дольше)
STEP = 20
CAD_PREFIX = "91:02"       # оставляем только номера с этим кварталом
WORKERS = 5                # потоков для этапа 1
RETRIES = 3                # попыток на один запрос
NSPD_PAUSE = 2             # пауза между запросами к НСПД, сек

CAD_LIST_FILE = "cad_list.txt"
CSV_FILE = "gagarinsky_7km.csv"
GEOJSON_FILE = "analyzed.geojson"
ERRORS_FILE = "errors.txt"
FAILED_POINTS_FILE = "failed_points.txt"
# ===================================================

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://map.ru/pkk/sevastopol",
    "Origin": "https://map.ru"
}


# ---------- ЭТАП 1: сбор кад номеров по сетке ----------

def get_feature(x, y):
    """Возвращает (статус, данные). Статус: ok / empty / fail"""
    for attempt in range(RETRIES):
        try:
            r = requests.get(
                "https://map.ru/api/wms",
                params={"x": x, "y": y, "layers": "36048"},
                headers=HEADERS,
                timeout=12
            )
            if r.status_code == 200:
                data = r.json()
                features = data.get("features", [])
                if not features:
                    return "empty", None
                p = features[0].get("properties", {})
                cn = p.get("descr") or p.get("label") or p.get("cad_num")
                if cn and CAD_PREFIX in str(cn):
                    return "ok", {
                        "cn": str(cn).strip(),
                        "area": p.get("area"),
                        "category": p.get("categoryName"),
                        "cost": p.get("cost_value")
                    }
                return "empty", None
            # 429, 5xx и т.д. - ждем и пробуем снова
            time.sleep(1 + attempt * 2)
        except Exception:
            time.sleep(1 + attempt * 2)
    return "fail", None


def collect_cad_numbers():
    minx = CENTER_X - SIZE_METERS / 2
    miny = CENTER_Y - SIZE_METERS / 2
    count = int(SIZE_METERS / STEP) + 1

    # считаем через индексы, чтобы не копились ошибки float
    points = []
    for i in range(count):
        for j in range(count):
            points.append((round(minx + i * STEP, 2),
                          round(miny + j * STEP, 2)))

    print(f"Точек для проверки: {len(points):,}")
    print(f"Квадрат: {SIZE_METERS}x{SIZE_METERS} м, шаг: {STEP} м\n")

    results = {}        # кад номер -> данные (дубли отсекаются автоматически)
    failed_points = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_to_point = {executor.submit(
            get_feature, x, y): (x, y) for x, y in points}

        for future in tqdm(as_completed(future_to_point), total=len(points)):
            status, data = future.result()
            if status == "ok":
                if data["cn"] not in results:
                    results[data["cn"]] = data
            elif status == "fail":
                failed_points.append(future_to_point[future])

    cad_numbers = sorted(results.keys())

    with open(CAD_LIST_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(cad_numbers))

    with open(CSV_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["cn", "area", "category", "cost"])
        writer.writeheader()
        for cn in cad_numbers:
            writer.writerow(results[cn])

    with open(FAILED_POINTS_FILE, "w", encoding="utf-8") as f:
        for x, y in failed_points:
            f.write(f"{x},{y}\n")

    print(f"\nЭтап 1 готов. Уникальных участков: {len(cad_numbers)}")
    print(
        f"Точек с ошибкой запроса (после {RETRIES} попыток): {len(failed_points)}")
    return cad_numbers


# ---------- ЭТАП 2: проверка через НСПД и сохранение в GeoJSON ----------

def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def get_first_existing(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return 0


def find_with_retry(nspd, cad):
    """Ищет участок в НСПД, при ошибке сети повторяет запрос"""
    last_error = None
    for attempt in range(RETRIES):
        try:
            return nspd.find(cad)
        except Exception as e:
            last_error = e
            time.sleep(3 + attempt * 3)
    raise last_error


def build_geojson(cads):
    area_keys = ["specified_area", "declared_area",
                 "land_record_area", "ed_area"]
    cost_keys = ["cost_value", "cad_cost", "cadastral_cost"]

    features = []
    errors = []

    with Nspd() as nspd:  # один клиент на весь цикл
        for i, cad in enumerate(cads, 1):
            print(f"Обрабатываю {i}/{len(cads)} -> {cad}")
            try:
                feat = find_with_retry(nspd, cad)
                if not feat or not feat.geometry:
                    raise ValueError("Нет геометрии")

                geojson_geom = mapping(feat.geometry.to_shape())

                # mode="json" чтобы даты и прочее нормально писались в json
                props = feat.properties.options.model_dump(mode="json")
                props["cad_number"] = cad

                area = safe_float(get_first_existing(props, area_keys))
                cost = safe_float(get_first_existing(props, cost_keys))

                props["area_m2"] = area
                props["cadastral_cost"] = cost
                props["price_m2"] = round(
                    cost / area, 2) if area > 0 and cost > 0 else None

                features.append({
                    "type": "Feature",
                    "geometry": geojson_geom,
                    "properties": props
                })

            except Exception as e:
                print(f"Ошибка с {cad}: {e}")
                errors.append(cad)

            time.sleep(NSPD_PAUSE)  # лимит НСПД

    final_geojson = {"type": "FeatureCollection", "features": features}
    with open(GEOJSON_FILE, "w", encoding="utf-8") as f:
        json.dump(final_geojson, f, ensure_ascii=False, indent=2, default=str)

    with open(ERRORS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(errors))

    print(f"\nГотово! Успешно: {len(features)}, ошибок: {len(errors)}")


def main():
    cads = collect_cad_numbers()
    if not cads:
        print("Кад номера не найдены, этап 2 пропущен")
        return
    build_geojson(cads)


if __name__ == "__main__":
    main()
