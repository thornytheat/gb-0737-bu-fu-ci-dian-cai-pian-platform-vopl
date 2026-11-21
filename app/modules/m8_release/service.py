from collections import Counter

from app.config import MAX_PIECES, VERSION
from app.reasons import FabricError


def release_document(run_id, snapshot, cancel_id, substitute, release_id, created_at):
    """在归档快照上生成撤单释放单，不改动原批次。每片撤单裁片转用或隔离，未裁段回退可用。"""
    kerf = snapshot["fit"]["kerf_mm"]
    planning = snapshot["changeovers"]
    books = {book["roll"]: book for book in snapshot["ledger"]["rolls"]}
    rolls = {roll["id"]: roll for roll in snapshot["zones"]["rolls"]}

    known = {item["order"] for item in planning["orders"]}
    if cancel_id not in known:
        raise FabricError("CANCEL_UNKNOWN")
    if substitute["id"] in known:
        raise FabricError("SUBSTITUTE_CLASH")
    if substitute["quantity"] > MAX_PIECES:
        raise FabricError("PLAN_LIMIT")
    pieces = [item for item in planning["placements"] if item["order"] == cancel_id]
    if not pieces:
        raise FabricError("CANCEL_NO_PIECES")

    length = substitute["net"] + 2 * substitute["seam"]
    sequence = {name: index for index, name in enumerate(planning["roll_sequence"])}
    released = sorted({item["roll"] for item in pieces}, key=lambda name: sequence[name])

    def usable(roll_id):
        roll = rolls[roll_id]
        return roll["fabric"] == substitute["fabric"] and roll["width"] >= substitute["width"]

    if not any(usable(roll_id) for roll_id in released):
        raise FabricError("SUBSTITUTE_INCOMPATIBLE")

    # 每片撤单裁片：裁长、布种、幅宽一致才转用，坐标不变；其余连同刀口隔离待处置。
    dispositions, transferable = [], 0
    ordered = sorted(pieces, key=lambda item: (sequence[item["roll"]], item["start"]))
    for item in ordered:
        if usable(item["roll"]) and item["length"] == length and transferable < substitute["quantity"]:
            disposition, transferable = "transferred", transferable + 1
        else:
            disposition = "quarantined"
        dispositions.append({"piece": item["id"], "roll": item["roll"], "start": item["start"], "end": item["end"],
                             "kerf_end": item["kerf_end"], "length": item["length"], "disposition": disposition,
                             "as": None, "source": item["source"]})

    # 替代签片编号：先转用后新裁，转用片保留原卷原段。
    transfers, sub_pieces = {}, []
    for item in dispositions:
        if item["disposition"] == "transferred":
            sub_id = f"{substitute['id']}/{len(sub_pieces) + 1:02}"
            transfers[item["piece"]] = sub_id
            item["as"] = sub_id
            sub_pieces.append({"id": sub_id, "origin": "transferred", "from_piece": item["piece"],
                               "roll": item["roll"], "start": item["start"], "end": item["end"],
                               "kerf_start": item["end"], "kerf_end": item["kerf_end"], "length": item["length"]})

    # 释放卷上的未裁段回到可用料，替代单欠量按领卷次序从回退段新裁，每片仍计紧随刀口。
    remaining = substitute["quantity"] - len(sub_pieces)
    returned, span_cuts = [], {}
    for roll_id in released:
        segments = []
        for span in books[roll_id]["spans"]:
            if span["type"] != "unused":
                continue
            segment = {"start": span["start"], "end": span["end"], "length": span["end"] - span["start"],
                       "consumed_by": [], "remaining": {"start": span["start"], "end": span["end"]}}
            cursor = span["start"]
            if remaining and usable(roll_id):
                while remaining and cursor + length + kerf <= span["end"]:
                    sub_id = f"{substitute['id']}/{len(sub_pieces) + 1:02}"
                    cut = {"id": sub_id, "origin": "cut", "from_piece": None, "roll": roll_id,
                           "start": cursor, "end": cursor + length, "kerf_start": cursor + length,
                           "kerf_end": cursor + length + kerf, "length": length}
                    sub_pieces.append(cut)
                    span_cuts.setdefault((roll_id, span["start"]), []).append(cut)
                    segment["consumed_by"].append(sub_id)
                    cursor += length + kerf
                    remaining -= 1
            segment["remaining"] = {"start": cursor, "end": span["end"]} if cursor < span["end"] else None
            segments.append(segment)
        returned.append({"roll": roll_id, "segments": segments})

    # 释放前后逐卷勾稽：卷长等于合格、禁切、切口、隔离与未用段之和。
    quarantined = {item["piece"] for item in dispositions if item["disposition"] == "quarantined"}
    reconciliation = []
    for roll_id in released:
        book = books[roll_id]
        intervals = []
        for span in book["spans"]:
            kind, piece_id = span["type"], span.get("id")
            if kind == "unused":
                cuts = span_cuts.get((roll_id, span["start"]), [])
                for cut in cuts:
                    intervals.append({"start": cut["start"], "end": cut["end"], "type": "piece",
                                      "id": cut["id"], "order": substitute["id"]})
                    intervals.append({"start": cut["kerf_start"], "end": cut["kerf_end"], "type": "kerf", "id": cut["id"]})
                tail = cuts[-1]["kerf_end"] if cuts else span["start"]
                if tail < span["end"]:
                    intervals.append({"start": tail, "end": span["end"], "type": "unused"})
            elif kind in ("piece", "kerf") and piece_id in quarantined:
                intervals.append({"start": span["start"], "end": span["end"], "type": "quarantined", "id": piece_id})
            elif kind in ("piece", "kerf") and piece_id in transfers:
                updated = {"start": span["start"], "end": span["end"], "type": kind, "id": transfers[piece_id]}
                if kind == "piece":
                    updated["order"] = substitute["id"]
                intervals.append(updated)
            else:
                intervals.append({"start": span["start"], "end": span["end"], "type": kind})
        cursor, sums = 0, Counter()
        for span in sorted(intervals, key=lambda item: (item["start"], item["end"])):
            if span["start"] != cursor or not span["start"] < span["end"]:
                raise RuntimeError("释放后卷段重叠或断档")
            sums[span["type"]] += span["end"] - span["start"]
            cursor = span["end"]
        if cursor != book["length"]:
            raise RuntimeError("释放后卷长不守恒")
        before, after = book["lengths"], {key: sums[key] for key in ("piece", "blocked", "kerf", "quarantined", "unused")}
        if after["blocked"] != before["blocked"] or sum(after.values()) != book["length"]:
            raise RuntimeError("释放勾稽不守恒")
        reconciliation.append({"roll": roll_id, "length": book["length"], "before": before, "after": after, "delta": 0})

    totals_before = {key: sum(item["before"][key] for item in reconciliation) for key in ("piece", "blocked", "kerf", "unused")}
    totals_after = {key: sum(item["after"][key] for item in reconciliation)
                    for key in ("piece", "blocked", "kerf", "quarantined", "unused")}
    delta = sum(totals_before.values()) - sum(totals_after.values())
    if delta != 0 or sorted(item["piece"] for item in dispositions) != sorted(item["id"] for item in pieces):
        raise RuntimeError("撤单裁片处置遗漏")
    if len(sub_pieces) + remaining != substitute["quantity"]:
        raise RuntimeError("替代签片数量不守恒")

    manifest = {"id": release_id, "run": run_id, "created_at": created_at, "rules": VERSION, "kerf_mm": kerf,
                "cancel_order": cancel_id, "substitute": {**substitute, "length": length, "priority": "急"},
                "released_rolls": released, "transferred": len(transfers), "quarantined": len(quarantined),
                "cut_new": substitute["quantity"] - len(transfers) - remaining, "short": remaining}
    return {"manifest": manifest, "pieces": dispositions, "returned": returned,
            "substitute_pieces": sub_pieces, "reconciliation": reconciliation,
            "totals": {"before": totals_before, "after": totals_after, "delta": delta}}
