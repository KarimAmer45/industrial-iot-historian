<p align="center"><img src="docs/figures/hero.png" alt="Industrial IoT Historian: 83,266 OPC UA changes stored, 3 of 486 simulated carriers flagged with the planted 70 percent charge fault, 57 to 1 compression on the simulated rectifier series" width="100%"></p>

<p align="center"><a href="https://github.com/KarimAmer45/industrial-iot-historian/actions/workflows/verify.yml"><img src="https://github.com/KarimAmer45/industrial-iot-historian/actions/workflows/verify.yml/badge.svg" alt="CI"></a> <img src="https://img.shields.io/badge/TimescaleDB-2.30-FDB515?style=flat-square&logo=timescale&logoColor=white" alt="TimescaleDB"> <img src="https://img.shields.io/badge/PostgreSQL-17-4169E1?style=flat-square&logo=postgresql&logoColor=white" alt="PostgreSQL 17"> <img src="https://img.shields.io/badge/OPC_UA-asyncua-0A5C8A?style=flat-square&logo=python&logoColor=white" alt="OPC UA"> <img src="https://img.shields.io/badge/Grafana-13-F46800?style=flat-square&logo=grafana&logoColor=white" alt="Grafana"> <img src="https://img.shields.io/badge/Docker-ready-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker"></p>
<p align="center">Plating Data Platform: <a href="https://github.com/KarimAmer45/manufacturing-traceability">Traceability</a> &middot; <a href="https://github.com/KarimAmer45/process-quality-analytics">Lab ETL & SPC</a> &middot; <b>IoT historian</b> &middot; <a href="https://github.com/KarimAmer45/coating-specification-engine">Specification & release</a></p>

A plating line's rectifiers decide how much metal goes on each part. This project replays real sensor recordings over OPC UA into TimescaleDB, handles the gaps an industrial collector really sees, and turns rectifier current into charge per carrier in SQL, which is how a carrier that received too little current shows up.

> **Kurzfassung:** Echte Sensoraufzeichnungen (SKAB-Prüfstand, UCI-Stahlwerkszähler) werden über OPC UA in TimescaleDB abgespielt; Ausfälle und ungültige Messwerte werden als Lücken gespeichert, nicht überbrückt. Simulierte Gleichrichterströme werden je Warenträger zu Amperestunden integriert; ein absichtlich eingebauter Fehler mit 70 % Strom zeigt sich so bei genau den 3 betroffenen von 486 Warenträgern.

<p align="center"><img src="docs/figures/pipeline.png" alt="Acquisition path: real recordings, OPC UA server, collector with gap markers, TimescaleDB hypertable, SQL and Grafana" width="100%"></p>

<p align="center"><img src="docs/figures/carrier_charge.png" alt="Measured charge as a share of planned charge for 486 carriers; three carriers at 70 percent" width="100%"></p>

<p align="center"><img src="docs/results/grafana_rectifier.png" alt="Grafana dashboard with the rectifier week and the carrier charge table" width="100%"></p>

<p align="center"><img src="docs/figures/compression.png" alt="Columnstore compression per dataset: 57.5 to 1, 7.4 to 1, 1.7 to 1" width="70%"></p>

## Inside the database

- **OPC UA, the real way.** An asyncua server replays the recordings at their recorded times; the collector subscribes over TCP and stores only value changes, with the source timestamp in the primary key, so a repeat replay inserts nothing twice.
- **Honest gaps.** Bad status values, lost connections, queue overflow and collector stops become gap markers; the reconstruction views hold values forward but never across a gap.
- **TimescaleDB done properly.** One day chunks, columnstore segmented by signal, a conversion policy and an hourly continuous aggregate that matches all 2.3 million raw samples.
- **Energy in SQL.** Current is integrated over each carrier's exact dwell window into ampere hours and compared with the planned charge.
- **Least privilege.** Grafana reads through a dashboard role that can only see the six panels' tables and views; 42 tests on every push.

## Run it

```bash
cp .env.example .env    # set a local password
docker compose up -d db
docker compose run --rm demo
```

The demo downloads the pinned SKAB and UCI Steel files, runs the OPC UA replay and writes the evidence to `docs/results/`. Grafana and the native Windows setup are in the [report](REPORT.md).

## Data

SKAB (a water circulation test rig) and the UCI Steel Industry meter are **real** public recordings; the rectifier currents and carrier schedule are **simulated**. SKAB's pump current is never relabeled as a rectifier.

---

**Full technical report:** [REPORT.md](REPORT.md) has every result table, the evidence file behind each number, how each part is run and the known limitations.

Built by [Karim Amer](https://github.com/KarimAmer45), M.Sc. Mobile Robotics student at the University of Bonn.
