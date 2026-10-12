-- V005 traceability: order -> lot -> load (Warentraeger) -> steps -> measurements -> delivery -> complaint
CREATE TABLE trace.customer_order (
  order_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id int    NOT NULL REFERENCES ref.customer,
  order_no    text   NOT NULL,
  received_on date   NOT NULL,
  UNIQUE (customer_id, order_no)
);

CREATE TABLE trace.lot (
  lot_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lot_no          text   NOT NULL UNIQUE,
  order_id        bigint NOT NULL REFERENCES trace.customer_order,
  part_id         int    NOT NULL REFERENCES ref.part,
  customer_lot_no text,                          -- batch number on the customer's delivery note
  spec_code       text,                          -- coating designation as ordered; project 4 links it to spec tables
  qty_pcs         int    NOT NULL CHECK (qty_pcs > 0),
  parent_lot_id   bigint REFERENCES trace.lot,
  origin          text   NOT NULL DEFAULT 'receipt' CHECK (origin IN ('receipt','split','rework')),
  received_at     timestamptz NOT NULL,
  status          text   NOT NULL DEFAULT 'received'
                  CHECK (status IN ('received','in_process','blocked','reworked','released','shipped','scrapped')),
  CHECK ((origin = 'receipt') = (parent_lot_id IS NULL)),
  CHECK (parent_lot_id IS DISTINCT FROM lot_id)
);
CREATE INDEX lot_parent_idx ON trace.lot (parent_lot_id) WHERE parent_lot_id IS NOT NULL;
CREATE INDEX lot_order_idx  ON trace.lot (order_id);

CREATE TRIGGER lot_audit AFTER INSERT OR UPDATE OR DELETE ON trace.lot
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('lot_id');

-- One run of one rack or barrel through the line
CREATE TABLE trace.load (
  load_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  line_id     int    NOT NULL REFERENCES ref.line,
  carrier_no  text   NOT NULL,                  -- physical rack or barrel number
  started_at  timestamptz NOT NULL,
  finished_at timestamptz,
  program     text,
  CHECK (finished_at IS NULL OR finished_at > started_at)
);
CREATE INDEX load_started_idx ON trace.load (started_at);

-- Many to many: one carrier can hold several lots, one lot can be spread over several carriers
CREATE TABLE trace.load_lot (
  load_id bigint NOT NULL REFERENCES trace.load ON DELETE CASCADE,
  lot_id  bigint NOT NULL REFERENCES trace.lot,
  qty_pcs int    NOT NULL CHECK (qty_pcs > 0),
  PRIMARY KEY (load_id, lot_id)
);
CREATE INDEX load_lot_lot_idx ON trace.load_lot (lot_id);

-- Dwell of a load in a station. Physical rules enforced by the database:
--   a station holds one carrier at a time, a carrier is in one station at a time.
CREATE TABLE trace.load_step (
  step_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  load_id    bigint NOT NULL REFERENCES trace.load ON DELETE CASCADE,
  station_id int    NOT NULL REFERENCES ref.station,
  in_station tstzrange NOT NULL CHECK (NOT isempty(in_station) AND NOT upper_inf(in_station)),
  amp_hours  numeric(12,3) CHECK (amp_hours >= 0),     -- filled by project 3 from rectifier data
  CONSTRAINT station_one_carrier EXCLUDE USING gist (station_id WITH =, in_station WITH &&),
  CONSTRAINT carrier_one_station EXCLUDE USING gist (load_id   WITH =, in_station WITH &&)
);

CREATE TABLE trace.measurement (
  measurement_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lot_id         bigint NOT NULL REFERENCES trace.lot,
  load_id        bigint REFERENCES trace.load,
  measured_at    timestamptz NOT NULL,
  characteristic text   NOT NULL CHECK (characteristic IN
                 ('thickness_um','ni_pct','adhesion','appearance','nss_h_white','nss_h_red','friction_coeff')),
  sample_no      int    NOT NULL DEFAULT 1 CHECK (sample_no > 0),
  location       text,                                  -- measuring point on the part
  value          numeric(12,4),
  result_text    text,
  method         text   NOT NULL,                       -- 'XRF ISO 3497', 'NSS ISO 9227', ...
  CHECK (value IS NOT NULL OR result_text IS NOT NULL)
);
CREATE INDEX measurement_lot_idx ON trace.measurement (lot_id, characteristic);

CREATE TRIGGER measurement_audit AFTER INSERT OR UPDATE OR DELETE ON trace.measurement
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('measurement_id');

CREATE TABLE trace.delivery (
  delivery_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id int    NOT NULL REFERENCES ref.customer,
  delivery_no text   NOT NULL UNIQUE,
  shipped_at  timestamptz NOT NULL
);

CREATE TABLE trace.delivery_lot (
  delivery_id bigint NOT NULL REFERENCES trace.delivery ON DELETE CASCADE,
  lot_id      bigint NOT NULL REFERENCES trace.lot,
  qty_pcs     int    NOT NULL CHECK (qty_pcs > 0),
  PRIMARY KEY (delivery_id, lot_id)
);
CREATE INDEX delivery_lot_lot_idx ON trace.delivery_lot (lot_id);

CREATE TABLE trace.complaint (
  complaint_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id  int    NOT NULL REFERENCES ref.customer,
  lot_id       bigint REFERENCES trace.lot,
  delivery_id  bigint REFERENCES trace.delivery,
  received_on  date   NOT NULL,
  defect       text   NOT NULL,                   -- 'low thickness', 'white rust', 'blistering', ...
  status       text   NOT NULL DEFAULT 'open' CHECK (status IN ('open','containment','analysis','closed')),
  CHECK (lot_id IS NOT NULL OR delivery_id IS NOT NULL)
);

CREATE TRIGGER complaint_audit AFTER INSERT OR UPDATE OR DELETE ON trace.complaint
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('complaint_id');
