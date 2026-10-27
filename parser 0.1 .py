import json
import time
from pynspd import Nspd
from shapely.geometry import mapping   # для преобразования в GeoJSON

with open("cad_list.txt", "r", encoding="utf-8") as f:
    cads = [line.strip() for line in f if line.strip()]


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


features = []
errors = []

with Nspd() as nspd:                     # один контекст на весь цикл (быстрее)
    for i, cad in enumerate(cads, 1):
        print(f"Обрабатываю {i}/{len(cads)} -> {cad}")
        try:
            feat = nspd.find(cad)
            if not feat or not feat.geometry:
                raise ValueError("Нет геометрии")

            # Геометрия в GeoJSON-формате
            shape = feat.geometry.to_shape()
            geojson_geom = mapping(shape)

            # Свойства
            props = feat.properties.options.model_dump()          # сырые данные
            # или props = feat.properties.cast().options.model_dump_human_readable()  # человекочитаемые

            props["cad_number"] = cad

            area_keys = ["specified_area", "ed_area"]
            cost_keys = ["cost_value", "cad_cost", "cadastral_cost"]

            def get_first_existing(d, keys):
                for k in keys:
                    if k in d and d[k] not in (None, ""):
                        return d[k]
                return 0

            area = safe_float(get_first_existing(props, area_keys))
            cost = safe_float(get_first_existing(props, cost_keys))

            props["area_m2"] = area
            props["cadastral_cost"] = cost

            props["price_m2"] = round(
                cost / area, 2) if area > 0 and cost > 0 else None

            feature = {
                "type": "Feature",
                "geometry": geojson_geom,
                "properties": props
            }
            features.append(feature)

        except Exception as e:
            print(f"Ошибка с {cad}: {e}")
            errors.append(cad)

        time.sleep(2)   # лимит НСПД всё равно есть

final_geojson = {"type": "FeatureCollection", "features": features}
with open("analyzed.geojson", "w", encoding="utf-8") as f:
    json.dump(final_geojson, f, ensure_ascii=False, indent=2)

with open("errors.txt", "w") as f:
    f.write("\n".join(errors))

print(f"Готово! Успешно: {len(features)}, ошибок: {len(errors)}")
