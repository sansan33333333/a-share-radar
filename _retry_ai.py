import json
import os
import ai
import config
import store

config.setup_stdout()
d = json.loads(config.LATEST_PATH.read_text(encoding="utf-8"))
before = d["generated_at"]
ai.run_ai(d)
print(json.dumps(d["ai"], ensure_ascii=False, indent=2))
print("reviews", [(p["code"], p.get("review", {}).get("status")) for p in d.get("cong", {}).get("items", [])])
if d["ai"]["status"] != "ok":
    raise SystemExit(1)
current = json.loads(config.LATEST_PATH.read_text(encoding="utf-8"))
if current["generated_at"] != before:
    raise SystemExit("New report exists; not overwriting")
tmp = config.LATEST_PATH.with_suffix(".retry.tmp")
tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
os.replace(tmp, config.LATEST_PATH)
store.save(d)
