#!/usr/bin/python3
"""GridTracker2 Call Roster alert hook — optional feed into the call queue.

Install as ~/.config/GridTracker2/Call Roster Scripts/cr-alert.sh
(chmod 755) and click Script Enabled in the roster the first time it appears.

By default this only POSTs an alert. Set ENQUEUE=1 if you want every new
wanted station added to the queue automatically — that is more aggressive
than picking stations by hand, so leave it off unless that is what you want.
The companion still will not transmit until you Arm it.
"""
import json
import os
import urllib.request

ENQUEUE = os.environ.get("ENQUEUE", "") == "1"
URL = os.environ.get("QUEUE_URL", "http://127.0.0.1:8765/api/from-alert")

with open("cr-alert.json", encoding="utf-8") as f:
    data = json.load(f)

for call, sta in data.items():
    if not (sta.get("shouldRosterAlert") and not sta.get("rosterAlerted")):
        continue
    # Prefer stations that are actually calling CQ, if GridTracker provides it.
    calling = str(sta.get("calling") or sta.get("CQ") or "")
    note = " ".join(
        str(x) for x in (
            sta.get("hunting"), sta.get("dxccName"), sta.get("grid"),
            sta.get("band"), sta.get("mode"),
        ) if x
    )
    body = json.dumps({
        "call": call,
        "note": note,
        "calling": calling,
        "enqueue": ENQUEUE,
    }).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            URL, data=body, headers={"Content-Type": "application/json"}
        ), timeout=2)
    except OSError as exc:
        print(f"queue post failed for {call}: {exc}")
