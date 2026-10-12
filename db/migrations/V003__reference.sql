-- V003 master data
CREATE TABLE ref.customer (
  customer_id int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code        text NOT NULL UNIQUE,              -- anonymised, e.g. 'C001'
  name        text NOT NULL                      -- never a real company name in the public repo
);

CREATE TABLE ref.process (
  process_code text PRIMARY KEY,                 -- 'ZNNI_ALK', 'ZN_ALK', 'PHOS_ZN', ...
  description  text NOT NULL
);

CREATE TABLE ref.line (
  line_id  int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code     text NOT NULL UNIQUE,                 -- 'L1'
  handling text NOT NULL CHECK (handling IN ('barrel','rack'))   -- Trommel / Gestell
);

-- A tank holds one solution (a bath). Chemistry tanks carry a process_code; rinses do not.
CREATE TABLE ref.tank (
  tank_id      int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  line_id      int  NOT NULL REFERENCES ref.line,
  position     int  NOT NULL CHECK (position > 0),
  code         text NOT NULL UNIQUE,             -- 'L1-T07'
  function     text NOT NULL CHECK (function IN
               ('degrease','pickle','rinse','activate','plate','passivate','seal','dry','strip','other')),
  process_code text REFERENCES ref.process,
  volume_l     numeric(10,1) CHECK (volume_l > 0),
  UNIQUE (line_id, position),
  CHECK (function NOT IN ('plate','passivate','seal') OR process_code IS NOT NULL)
);

-- A station is one carrier slot inside a tank (a long plating tank often has several).
CREATE TABLE ref.station (
  station_id int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tank_id    int  NOT NULL REFERENCES ref.tank,
  slot       int  NOT NULL CHECK (slot > 0),
  code       text NOT NULL UNIQUE,               -- 'L1-T07-S2'
  UNIQUE (tank_id, slot)
);

CREATE TABLE ref.part (
  part_id       int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id   int  NOT NULL REFERENCES ref.customer,
  part_no       text NOT NULL,
  description   text,
  base_material text NOT NULL DEFAULT 'steel',
  mass_g        numeric(10,3) CHECK (mass_g > 0),
  area_dm2      numeric(10,5) CHECK (area_dm2 > 0),
  data_source   text NOT NULL,                   -- key into config/sources.yaml
  UNIQUE (customer_id, part_no)
);

CREATE TRIGGER part_audit AFTER INSERT OR UPDATE OR DELETE ON ref.part
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('part_id');
