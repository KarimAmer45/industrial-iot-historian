-- V006 traceability functions

-- Every ancestor and descendant of a lot (rework and split genealogy).
-- CYCLE stops the recursion if bad data ever creates a loop.
CREATE OR REPLACE FUNCTION trace.lot_family(p_lot_id bigint)
RETURNS TABLE (lot_id bigint, lot_no text, relation text, depth int)
LANGUAGE sql STABLE AS $$
  WITH RECURSIVE
  up (lot_id, parent_lot_id, depth) AS (
    SELECT l.lot_id, l.parent_lot_id, 0 FROM trace.lot l WHERE l.lot_id = p_lot_id
    UNION ALL
    SELECT p.lot_id, p.parent_lot_id, up.depth - 1
      FROM trace.lot p JOIN up ON p.lot_id = up.parent_lot_id
  ) CYCLE lot_id SET up_cycle USING up_path,
  down (lot_id, depth) AS (
    SELECT l.lot_id, 0 FROM trace.lot l WHERE l.lot_id = p_lot_id
    UNION ALL
    SELECT c.lot_id, down.depth + 1
      FROM trace.lot c JOIN down ON c.parent_lot_id = down.lot_id
  ) CYCLE lot_id SET down_cycle USING down_path,
  fam AS (
    SELECT up.lot_id, up.depth FROM up WHERE NOT up_cycle
    UNION
    SELECT down.lot_id, down.depth FROM down WHERE NOT down_cycle
  )
  SELECT fam.lot_id, l.lot_no,
         CASE WHEN fam.depth < 0 THEN 'ancestor' WHEN fam.depth > 0 THEN 'descendant' ELSE 'self' END,
         fam.depth
    FROM fam JOIN trace.lot l ON l.lot_id = fam.lot_id
   ORDER BY fam.depth, fam.lot_id;
$$;

-- Backward trace: lot (and the lots it was reworked from) -> carriers -> chemistry tanks -> every
-- bath analysis that was in force while the carrier sat in that tank.
CREATE OR REPLACE FUNCTION trace.backward_trace(p_lot_id bigint)
RETURNS TABLE (lot_no text, relation text, load_id bigint, carrier_no text, station_code text,
               tank_code text, in_station tstzrange, analysis_id bigint, parameter_code text,
               value numeric, status text)
LANGUAGE sql STABLE AS $$
  SELECT l.lot_no, f.relation, ld.load_id, ld.carrier_no, s.code, t.code, st.in_station,
         rs.analysis_id, rs.parameter_code, rs.value, rs.status
    FROM trace.lot_family(p_lot_id) f
    JOIN trace.lot l        ON l.lot_id = f.lot_id
    JOIN trace.load_lot ll  ON ll.lot_id = l.lot_id
    JOIN trace.load ld      ON ld.load_id = ll.load_id
    JOIN trace.load_step st ON st.load_id = ld.load_id
    JOIN ref.station s      ON s.station_id = st.station_id
    JOIN ref.tank t         ON t.tank_id = s.tank_id
    LEFT JOIN lab.v_result_status rs
           ON rs.tank_id = t.tank_id
          AND rs.valid_during && st.in_station
   WHERE f.relation IN ('self','ancestor')
     AND t.process_code IS NOT NULL
   ORDER BY lower(st.in_station), t.code, rs.parameter_code;
$$;

-- Forward trace (containment): a tank was out of spec during p_window.
-- Which lots went through it, which lots were later made from them, and where did they ship?
CREATE OR REPLACE FUNCTION trace.forward_trace(p_tank_id int, p_window tstzrange)
RETURNS TABLE (lot_id bigint, lot_no text, relation text, lot_status text, customer_code text,
               delivery_no text, shipped_at timestamptz, qty_pcs int)
LANGUAGE sql STABLE AS $$
  WITH hit_lots AS (
    SELECT DISTINCT ll.lot_id
      FROM trace.load_step st
      JOIN ref.station s     ON s.station_id = st.station_id
      JOIN trace.load_lot ll ON ll.load_id = st.load_id
     WHERE s.tank_id = p_tank_id
       AND st.in_station && p_window
  ),
  affected AS (
    SELECT f.lot_id, f.relation
      FROM hit_lots h
      CROSS JOIN LATERAL trace.lot_family(h.lot_id) f
     WHERE f.relation IN ('self','descendant')
  )
  SELECT DISTINCT ON (a.lot_id, d.delivery_id)
         a.lot_id, l.lot_no, a.relation, l.status, c.code, d.delivery_no, d.shipped_at,
         coalesce(dl.qty_pcs, l.qty_pcs)
    FROM affected a
    JOIN trace.lot l               ON l.lot_id = a.lot_id
    JOIN trace.customer_order o    ON o.order_id = l.order_id
    JOIN ref.customer c            ON c.customer_id = o.customer_id
    LEFT JOIN trace.delivery_lot dl ON dl.lot_id = l.lot_id
    LEFT JOIN trace.delivery d      ON d.delivery_id = dl.delivery_id
   ORDER BY a.lot_id, d.delivery_id, (a.relation = 'self') DESC;
$$;
