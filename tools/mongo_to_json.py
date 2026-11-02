"""
One-time import of an existing Modmail MongoDB into the JSON database.
Only needed if this server was previously running on MongoDB.

    .venv/bin/python tools/mongo_to_json.py "mongodb+srv://user:pass@host/" [data_dir]

Run it while the bot is stopped (pm2 stop modmail).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient  # noqa: E402

from core.jsondb import JSONCollection, JSONDatabase, _json_default  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    uri = sys.argv[1]
    data_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "data")
    src = MongoClient(uri).modmail_bot
    dst = JSONDatabase(data_dir)

    for name in src.list_collection_names():
        coll: JSONCollection = dst.get_collection(name)
        n = 0
        for doc in src[name].find({}):
            doc["_id"] = str(doc["_id"])  # ObjectId -> string
            coll._store(json.loads(json.dumps(doc, default=_json_default)))
            n += 1
        print(f"{name}: {n} documents")
    print(f"Done. Data written to {dst.path}")


if __name__ == "__main__":
    main()
