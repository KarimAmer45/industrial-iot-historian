"""Project 1: simulate a ZnNi barrel line, driven only by config/sources.yaml.

What is real: line throughput (Partec website), bath operating window and current density
(supplier TDS), production bath ranges, deposition rates, deposit Ni content and lab
measurement uncertainty (NASF 2008 paper), part masses (ISO 4017 weight chart), coating
designations and thickness ranges (Partec norm pages).
What is assumed: everything under "assumptions" in sources.yaml, plus the planted incident.

Usage:
    python ingest/p1_simulate_plant.py --start 2026-03-02 --weeks 26 --seed 42
"""
import argparse
import math
import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from common import connect, reset_identity, sources

TZ = ZoneInfo("Europe/Berlin")


def part_area_dm2(d, l, s, k, thread_factor):
    """Surface of a hex screw as hex head plus cylinder shank, in dm2 (approximation)."""
    hex_face = math.sqrt(3) / 2 * s * s                  # area of a hexagon from width across flats
    side = s / math.sqrt(3)
    head = 2 * hex_face - math.pi * d * d / 4 + 6 * side * k
    shank = (math.pi * d * l + math.pi * d * d / 4) * thread_factor
    return (head + shank) / 10_000                        # mm2 -> dm2


def working_days(start: date, weeks: int, per_week: int):
    d = start
    while d < start + timedelta(weeks=weeks):
        if d.weekday() < per_week:
            yield d
        d += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat, default=date(2026, 3, 2))
    ap.add_argument("--weeks", type=int, default=26)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    if args.weeks < 1:
        ap.error('--weeks must be positive')
    rng = random.Random(args.seed)
    S = sources()
    A = S["assumptions"]
    sim = A["simulation"]["value"]
    zn = S["znni_alkaline"]
    obs = zn["production_observed"]["electrolyte_A"]

    takt_min = 60 / S["plant"]["carriers_per_hour"]["value"]
    rate_lo, rate_hi = obs["deposition_um_per_min_at_0_5_a_dm2"]
    rate_nominal = (rate_lo + rate_hi) / 2
    current_density = zn["current_density_a_dm2"]["barrel"][0]      # 0.5 A/dm2, where the rates were measured

    specs = []
    for sp in S["specs_for_simulation"]:
        t_min, t_max = sp["thickness_um"]
        aim = (t_min + t_max) / 2 if t_max else t_min + sim["open_range_margin_um"]
        specs.append({**sp, "t_min": t_min, "t_max": t_max, "aim": aim})
    max_dwell = max(sp["aim"] for sp in specs) / rate_nominal
    plate_slots = math.ceil(max_dwell / takt_min) + 1                 # derived, not assumed

    days = list(working_days(args.start, args.weeks, S["plant"]["working_days_per_week"]["value"]))
    if not days:
        ap.error('the selected interval has no working days')
    incident_day = days[len(days) // sim["incident_position_divisor"]]
    sample_time = time(sim["sample_hour"])
    incident = (datetime.combine(incident_day, sample_time, TZ),
                datetime.combine(incident_day + timedelta(days=sim["incident_days"]), sample_time, TZ))

    conn = connect("sim_p1")
    cur = conn.cursor()
    cur.execute("SELECT EXISTS(SELECT 1 FROM ref.process)")
    if cur.fetchone()[0]:
        raise SystemExit('Simulator requires an empty project database. Existing data was not changed.')

    # ---------- reference data ----------
    cur.execute("INSERT INTO ref.process VALUES ('ZNNI_ALK','Alkaline zinc nickel'), ('PASS_CR3','Cr(VI) free passivation')")
    cur.executemany("INSERT INTO lab.parameter VALUES (%s,%s,%s)", [
        ("ZN", "Zinc", "g/L"), ("NI", "Nickel", "g/L"), ("NAOH", "Sodium hydroxide", "g/L"),
        ("NA2CO3", "Sodium carbonate", "g/L"), ("COD", "Chemical oxygen demand", "g/L")])
    for p, lim in zn["bath_limits"].items():
        cur.execute("""INSERT INTO lab.parameter_limit (process_code, parameter_code, lsl, target, usl, valid_during, source)
                       VALUES ('ZNNI_ALK', %s, %s, %s, %s, daterange(%s, NULL), %s)""",
                    (p, lim.get("lsl"), lim.get("target"), lim.get("usl"), args.start, lim["src"]))

    cur.execute("INSERT INTO ref.line (code, handling) VALUES ('L1','barrel') RETURNING line_id")
    line_id = cur.fetchone()[0]
    tanks = [(1, "degrease", None), (2, "rinse", None), (3, "pickle", None), (4, "rinse", None),
             (5, "plate", "ZNNI_ALK"), (6, "rinse", None), (7, "passivate", "PASS_CR3"), (8, "rinse", None),
             (9, "dry", None)]
    tank_ids = {}
    for pos, fn, proc in tanks:
        cur.execute("INSERT INTO ref.tank (line_id, position, code, function, process_code) VALUES (%s,%s,%s,%s,%s) RETURNING tank_id",
                    (line_id, pos, f"L1-T{pos:02d}", fn, proc))
        tank_ids[pos] = cur.fetchone()[0]
    plate_tank = tank_ids[5]
    slot_ids = []
    for slot in range(1, plate_slots + 1):
        cur.execute("INSERT INTO ref.station (tank_id, slot, code) VALUES (%s,%s,%s) RETURNING station_id",
                    (plate_tank, slot, f"L1-T05-S{slot:02d}"))
        slot_ids.append(cur.fetchone()[0])

    customers = []
    for i, sp in enumerate(specs, start=1):
        cur.execute("INSERT INTO ref.customer (code, name) VALUES (%s,%s) RETURNING customer_id",
                    (f"C{i:03d}", f"Customer {i:03d}"))
        customers.append({"id": cur.fetchone()[0], "spec": sp, "parts": []})

    cat = S["parts_catalog"]
    for item in cat["items"]:
        area = part_area_dm2(item["d"], item["l"], item["s"], item["k"], A["thread_area_factor"]["value"])
        for c in rng.sample(customers, sim["customers_per_part"]):
            cur.execute("""INSERT INTO ref.part (customer_id, part_no, description, mass_g, area_dm2, data_source)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING part_id""",
                        (c["id"], f"{item['part_no']}", f"Hex screw {item['part_no']}",
                         item["kg_per_1000"], round(area, 5), f"{cat['mass_src']}+{cat['dims_src']}"))
            c["parts"].append({"id": cur.fetchone()[0], "kg_per_pc": item["kg_per_1000"] / 1000})
    customers = [c for c in customers if c["parts"]]

    # ---------- bath analyses: one per working day ----------
    # Centre: the TDS target (same source as the limits, so limits and values belong to one process).
    # Process spread: half the width of a real production range, read as +/- 2 sigma.
    # Lab noise: the published uncertainty of titration (Zn) and AAS (Ni).
    noise = zn["analysis_uncertainty_g_l"]
    lim = zn["bath_limits"]
    for d in days:
        t = datetime.combine(d, sample_time, TZ)
        vals = {p: rng.gauss(lim[p]["target"], (obs[p][1] - obs[p][0]) / 4) for p in ("ZN", "NI", "NAOH")}
        vals |= {p: rng.uniform(*obs[p]) for p in ("NA2CO3", "COD")}
        vals["ZN"] += rng.gauss(0, noise["ZN_titration"])
        vals["NI"] += rng.gauss(0, noise["NI_AAS"])
        if incident[0] <= t < incident[1]:
            vals["NI"] = zn["bath_limits"]["NI"]["lsl"] - rng.uniform(*sim["incident_ni_shortfall_g_l"])
        cur.execute("INSERT INTO lab.analysis (tank_id, sampled_at, source) VALUES (%s,%s,'sim:p1') RETURNING analysis_id",
                    (plate_tank, t))
        aid = cur.fetchone()[0]
        methods = {"ZN": "titration", "NI": "AAS", "NAOH": "titration", "NA2CO3": "titration", "COD": "COD"}
        cur.executemany("INSERT INTO lab.analysis_result VALUES (%s,%s,%s,%s)",
                        [(aid, p, round(v, 3), methods[p]) for p, v in vals.items()])

    # ---------- orders, lots, carriers, plating steps ----------
    barrel_kg = A["barrel_load_kg"]["value"]
    cv = A["within_carrier_cv"]["value"]
    lots, loads, load_lots, steps, meas = [], [], [], [], []
    slot_free = {sid: datetime.min.replace(tzinfo=TZ) for sid in slot_ids}
    lot_seq = load_seq = order_seq = 0
    ni_lo, ni_hi = obs["deposit_ni_pct"]

    for d in days:
        day_start = datetime.combine(d, time(0), TZ)
        queue = []
        for _ in range(A["lots_per_day"]["value"]):
            c = rng.choice(customers)
            part = rng.choice(c["parts"])
            pcs_per_carrier = int(barrel_kg / part["kg_per_pc"])
            n_carriers = rng.randint(*A["carriers_per_lot"]["value"])
            qty = int(pcs_per_carrier * (n_carriers - rng.random() * sim["partial_barrel_fraction"]))
            order_seq += 1
            cur.execute("INSERT INTO trace.customer_order (customer_id, order_no, received_on) VALUES (%s,%s,%s) RETURNING order_id",
                        (c["id"], f"PO-{order_seq:06d}", d))
            order_id = cur.fetchone()[0]
            lot_seq += 1
            lot = {"id": lot_seq, "no": f"L{d:%y%m%d}-{lot_seq:05d}", "order": order_id, "cust": c["id"], "part": part["id"],
                   "spec": c["spec"], "qty": qty, "received": day_start, "pcs_per_carrier": pcs_per_carrier,
                   "parent": None, "origin": "receipt", "loads": []}
            lots.append(lot)
            queue.append(lot)

        # fill barrels; a barrel may finish one lot and start the next (many to many),
        # but only when both lots need the same coating program
        queue.sort(key=lambda l: (l["spec"]["spec_code"], l["part"]))
        t = day_start + timedelta(hours=sim["production_start_hour"])
        open_load, room = None, 0
        for lot in queue:
            if open_load is not None and (open_load["spec"], open_load["part"]) != (lot["spec"]["spec_code"], lot["part"]):
                room = 0
            remaining = lot["qty"]
            while remaining > 0:
                if room == 0:
                    load_seq += 1
                    sid = min(slot_free, key=slot_free.get)
                    t = max(t + timedelta(minutes=takt_min), slot_free[sid])
                    dwell = lot["spec"]["aim"] / rate_nominal
                    # reading the published range as roughly +/- 2 sigma around its midpoint
                    rate = rng.gauss(rate_nominal, (rate_hi - rate_lo) / 4)
                    end = t + timedelta(minutes=dwell)
                    open_load = {"id": load_seq, "start": t, "end": end, "thick": rate * dwell, "lots": [],
                                 "spec": lot["spec"]["spec_code"], "part": lot["part"]}
                    loads.append(open_load)
                    steps.append((load_seq, sid, t, end))
                    slot_free[sid] = end
                    room = lot["pcs_per_carrier"]
                take = min(room, remaining)
                open_load["lots"].append((lot["id"], take))
                lot["loads"].append(open_load)
                remaining -= take
                room -= take

    for ld in loads:
        for lot_id, q in ld["lots"]:
            load_lots.append((ld["id"], lot_id, q))

    # measurements and release decision per lot
    deliveries = defaultdict(list)
    for lot in lots:
        first = lot["loads"][0]
        ni_bad = incident[0] <= first["start"] < incident[1]
        lot_thick = []
        for i in range(A["thickness_samples_per_lot"]["value"]):
            ld = rng.choice(lot["loads"])
            v = max(sim["measurement_floor_um"], rng.gauss(ld["thick"], ld["thick"] * cv))
            lot_thick.append(v)
            meas.append((lot["id"], ld["id"], ld["end"] + timedelta(minutes=sim["thickness_test_delay_min"]), "thickness_um", i + 1, round(v, 2), "XRF ISO 3497"))
        # Ni content is only checked on every third lot: the gap the planted complaint exploits
        if lot["id"] % A["ni_check_every_nth_lot"]["value"] == 0:
            ni = rng.uniform(*sim["incident_deposit_ni_pct"]) if ni_bad else rng.uniform(ni_lo, ni_hi)
            meas.append((lot["id"], first["id"], first["end"] + timedelta(minutes=sim["ni_test_delay_min"]), "ni_pct", 1, round(ni, 2), "XRF ISO 3497"))
        else:
            ni = None
        sp = lot["spec"]
        fail = min(lot_thick) < sp["t_min"] or (sp["t_max"] and max(lot_thick) > sp["t_max"]) or (ni is not None and ni < S["znni_alkaline"]["deposit_ni_pct"]["min"])
        lot["status"] = "reworked" if fail and rng.random() < sim["rework_probability"] else ("blocked" if fail else "shipped")
        lot["bad_ni_unseen"] = ni_bad and ni is None
        if lot["status"] == "shipped":
            ship_day = max(ld["end"] for ld in lot["loads"]).date() + timedelta(days=sim["shipping_delay_days"])
            deliveries[(lot["cust"], ship_day)].append(lot)        # one delivery per customer and day

    # ---------- write everything ----------
    cur.executemany("""INSERT INTO trace.lot (lot_id, lot_no, order_id, part_id, spec_code, qty_pcs, received_at, status)
                       OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [(l["id"], l["no"], l["order"], l["part"], l["spec"]["spec_code"], l["qty"], l["received"], l["status"]) for l in lots])
    cur.executemany("INSERT INTO trace.load (load_id, line_id, carrier_no, started_at, finished_at, program) OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,%s)",
                    [(ld["id"], line_id, f"B{(ld['id'] % sim['carrier_pool']) + 1:02d}", ld["start"], ld["end"], "ZNNI") for ld in loads])
    cur.executemany("INSERT INTO trace.load_lot VALUES (%s,%s,%s)", load_lots)
    cur.executemany("INSERT INTO trace.load_step (load_id, station_id, in_station) VALUES (%s,%s,tstzrange(%s,%s,'[)'))", steps)
    cur.executemany("""INSERT INTO trace.measurement (lot_id, load_id, measured_at, characteristic, sample_no, value, method)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""", meas)

    # rework: stripped and re-plated as a child lot (not simulated through the line again; extend this)
    for lot in [l for l in lots if l["status"] == "reworked"]:
        lot_seq += 1
        cur.execute("""INSERT INTO trace.lot (lot_id, lot_no, order_id, part_id, spec_code, qty_pcs, parent_lot_id, origin, received_at, status)
                       OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,%s,%s,'rework',%s,'received')""",
                    (lot_seq, lot["no"] + "-R1", lot["order"], lot["part"], lot["spec"]["spec_code"], lot["qty"], lot["id"],
                     max(ld["end"] for ld in lot["loads"]) + timedelta(days=sim["rework_delay_days"])))

    for n, ((cust, ship_day), dl) in enumerate(sorted(deliveries.items(), key=lambda kv: (kv[0][1], kv[0][0])), start=1):
        cur.execute("INSERT INTO trace.delivery (customer_id, delivery_no, shipped_at) VALUES (%s,%s,%s) RETURNING delivery_id",
                    (cust, f"LS-{n:06d}", datetime.combine(ship_day, time(sim["shipping_hour"]), TZ)))
        did = cur.fetchone()[0]
        cur.executemany("INSERT INTO trace.delivery_lot VALUES (%s,%s,%s)", [(did, l["id"], l["qty"]) for l in dl])

    # planted complaint: a low Ni lot that was never Ni tested reaches a customer and shows white rust
    victim = next((l for l in lots if l["bad_ni_unseen"] and l["status"] == "shipped"), None)
    if victim:
        cur.execute("""INSERT INTO trace.complaint (customer_id, lot_id, received_on, defect)
                       VALUES (%s,%s,%s,'white rust after short exposure')""",
                    (victim["cust"], victim["id"], victim["loads"][0]["end"].date() + timedelta(weeks=sim["complaint_delay_weeks"])))

    for table, col in [("trace.lot", "lot_id"), ("trace.load", "load_id")]:
        reset_identity(cur, table, col)
    conn.commit()
    print(f"plate slots (derived): {plate_slots}, lots: {lot_seq}, carriers: {load_seq}, "
          f"measurements: {len(meas)}, incident: {incident[0]:%Y-%m-%d} to {incident[1]:%Y-%m-%d}, "
          f"complaint lot: {victim['no'] if victim else None}")


if __name__ == "__main__":
    main()
