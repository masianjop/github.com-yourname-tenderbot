import json
from datetime import datetime, timezone

INPUT_FILE = "rostender_cookies.txt"
OUTPUT_FILE = "rostender_cookies.json"

cookies = []

with open(INPUT_FILE, encoding="utf-8") as f:
    for line in f:
        parts = line.strip().split("\t")
        if len(parts) < 4:
            continue

        name, value, domain, path = parts[:4]

        # нас интересуют только куки rostender.info
        if "rostender.info" not in domain:
            continue

        exp_str = parts[4] if len(parts) > 4 else ""
        exp_str = exp_str.strip()
        expires = 0

        if exp_str and not exp_str.lower().startswith("session"):
            # формат типа 2026-12-28T14:21:34.332Z
            if exp_str.endswith("Z"):
                exp_str = exp_str[:-1]
            try:
                dt = datetime.fromisoformat(exp_str)
            except ValueError:
                # на всякий случай, если без миллисекунд
                dt = datetime.fromisoformat(exp_str.split(".")[0])
            expires = int(dt.replace(tzinfo=timezone.utc).timestamp())

        cookies.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "expires": expires,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        })

state = {"cookies": cookies, "origins": []}

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)

print(f"Сохранил {len(cookies)} cookies в {OUTPUT_FILE}")

