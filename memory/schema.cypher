// =============================================================================
// Vixy memory graph — episodic + agent schema (FalkorDB, OpenCypher)
//
// One graph per user:  vixy_<user_id>   (a query can never reach another user; formerly veyra_<user_id>)
// The unified graph (vixy.graph.v1, extended additively by v2) adds provenance properties, Day/Person and the Dreamer's
// Pattern/LongTermMemory to these labels: see docs/GRAPH_SCHEMA.md (this file stays the
// executable reference for the labels below).
//
//   EPISODIC (what happened)                     SEMANTIC (what we believe)
//
//   (:Note)-[:DURING]->(:Visit)-[:AT]------------------------>(:Place)
//                         |  ^                                    ^
//                         |  +-[:NEXT]-(:Visit)                   | [:ABOUT]
//                         +-[:SAW]------------------->(:Tag)      |
//                                                              (:Fact)
//   (:Visit)<----------------[:SUPPORTED_BY]--------------------(:Fact)
//   (:Note) <----------------[:SUPPORTED_BY]--------------------(:Fact)   user-stated
//
// Conventions (FalkorDB specifics)
//   - Times are INT epoch milliseconds (*_ms). Temporal types can't be range-indexed.
//   - Properties are never NULL. Optional properties (marked ?) are simply absent.
//   - Writes use MERGE on id, so re-ingesting the same frames is idempotent.
//
// This file is executable: statements are separated by ';'.
//   Sections 1 (indexes) and 3 (queries) match memory/store.py.
//   Section 2 is an example graph (one day at Koufu) for illustration/tests.
// =============================================================================


// -----------------------------------------------------------------------------
// NODES
// -----------------------------------------------------------------------------
//
// (:Visit)  EPISODIC — one stay or transit, built from consecutive frames
//   id                 STRING    'vis_<hash>' (deterministic from first frame)
//   kind               STRING    'stay' | 'transit'
//   start_ms, end_ms   INT       epoch ms
//   local_date         STRING    'YYYY-MM-DD' in user tz
//   dow                INT       0=Mon .. 6=Sun
//   start_min          INT       minutes after local midnight
//   frames             INT       frames in the visit
//   place_type         STRING    food_court | restaurant | cafe | classroom | library | lab |
//                                office | gym | supermarket | shop | home | transit |
//                                outdoors | healthcare | worship | unknown
//   activity           STRING    eating | studying | working | exercising | shopping |
//                                commuting | at_home | unknown
//   tags               [STRING]  detected classes, most frequent first
//   sensitive          BOOL      true for healthcare / worship
//   people_max?        INT       anonymous headcount (max 'person' count in one frame)
//   lat?, lon?         FLOAT     visit centroid (only with GPS)
//
// (:Note)  EPISODIC — something the user said (audio transcript) or typed
//   id                 STRING    'note_<hash>'
//   text               STRING
//   ts_ms              INT
//   local_date         STRING
//   source             STRING    'voice'
//   kind               STRING    'note' | 'reminder'
//   status?            STRING    'open' | 'done'           (reminders)
//   trigger_place_type? STRING   e.g. 'supermarket'       (reminders)
//
// (:Place)  SEMANTIC — a physical location (created only with GPS)
//   id                 STRING    'plc_<random>'
//   place_type         STRING    as Visit.place_type
//   name?              STRING    user-given
//   name_source?       STRING    'user'
//   lat, lon           FLOAT     mean of its visits' centroids
//   geo                POINT     point({latitude, longitude}) for distance()
//   visits             INT       number of AT edges
//   sensitive          BOOL
//   created_ms         INT
//
// (:Tag)  SEMANTIC — a class the vision model can detect
//   name               STRING    e.g. 'laptop', 'food court'
//
// (:Fact)  SEMANTIC — a belief, always linked to its evidence
//   id                 STRING    'fact_freq_<place>' | 'fact_act_<place>' | 'fact_pref_<hash>'
//   kind               STRING    'frequent_place' | 'usual_activity' | 'preference'
//   source             STRING    'inferred' | 'user'
//   status             STRING    'active' | 'inactive'   (inactive = lost support, kept)
//   confidence         FLOAT     frequent_place: days/(days+2); usual_activity: share of days;
//                                user preference: 0.98
//   updated_ms         INT
//   -- frequent_place:  support_days INT, first_date STRING, last_date STRING,
//                       usual_arrival_min INT, dows [INT]
//   -- usual_activity:  activity STRING, support_days INT
//   -- preference:      statement STRING, positive BOOL
//
// -----------------------------------------------------------------------------
// RELATIONSHIPS
// -----------------------------------------------------------------------------
//
//   (:Visit)-[:AT]->(:Place)
//   (:Visit)-[:SAW {ratio FLOAT, conf FLOAT, max_count INT}]->(:Tag)
//        ratio = share of the visit's frames with the class, conf = mean confidence,
//        max_count = most instances in a single frame
//   (:Visit)-[:NEXT {gap_min INT}]->(:Visit)        next visit within 3 h
//   (:Note)-[:DURING]->(:Visit)                     visit happening when it was said
//   (:Fact)-[:ABOUT]->(:Place)
//   (:Fact)-[:SUPPORTED_BY]->(:Visit)               one visit per supporting day (<= 30)
//   (:Fact)-[:SUPPORTED_BY]->(:Note)                user-stated facts
//
// =============================================================================


// -----------------------------------------------------------------------------
// 1. INDEXES  (run once per user graph — MemoryStore.setup() does this)
// -----------------------------------------------------------------------------
CREATE INDEX FOR (n:Visit) ON (n.id);
CREATE INDEX FOR (n:Visit) ON (n.start_ms);
CREATE INDEX FOR (n:Visit) ON (n.local_date);
CREATE INDEX FOR (n:Note)  ON (n.id);
CREATE INDEX FOR (n:Note)  ON (n.ts_ms);
CREATE INDEX FOR (n:Place) ON (n.id);
CREATE INDEX FOR (n:Place) ON (n.geo);
CREATE INDEX FOR (n:Tag)   ON (n.name);
CREATE INDEX FOR (n:Fact)  ON (n.id);
CREATE INDEX FOR (n:Fact)  ON (n.status);


// -----------------------------------------------------------------------------
// 2. EXAMPLE GRAPH — lunch at Koufu on 3 days -> a frequent_place Fact
// -----------------------------------------------------------------------------
CREATE (p:Place {id: 'plc_koufu', place_type: 'food_court', name: 'Koufu', name_source: 'user',
                 lat: 1.3467, lon: 103.68, geo: point({latitude: 1.3467, longitude: 103.68}),
                 visits: 3, sensitive: false, created_ms: 1792814400000});

UNWIND [
  {id: 'vis_mon', start: 1792814100000, date: '2026-10-19', dow: 0},
  {id: 'vis_tue', start: 1792900500000, date: '2026-10-20', dow: 1},
  {id: 'vis_wed', start: 1792986900000, date: '2026-10-21', dow: 2}
] AS d
MATCH (p:Place {id: 'plc_koufu'})
CREATE (v:Visit {id: d.id, kind: 'stay', start_ms: d.start, end_ms: d.start + 3300000,
                 local_date: d.date, dow: d.dow, start_min: 695, frames: 210,
                 place_type: 'food_court', activity: 'eating',
                 tags: ['food court', 'person', 'table', 'plate', 'noodle'],
                 sensitive: false, people_max: 9, lat: 1.3467, lon: 103.68})
CREATE (v)-[:AT]->(p);

UNWIND [
  {name: 'food court', ratio: 0.9, conf: 0.82, n: 1},
  {name: 'noodle',     ratio: 0.8, conf: 0.77, n: 2},
  {name: 'person',     ratio: 0.95, conf: 0.93, n: 9}
] AS t
MERGE (g:Tag {name: t.name})
WITH g, t
MATCH (v:Visit)
CREATE (v)-[:SAW {ratio: t.ratio, conf: t.conf, max_count: t.n}]->(g);

MATCH (a:Visit {id: 'vis_mon'}), (b:Visit {id: 'vis_tue'}), (c:Visit {id: 'vis_wed'})
MATCH (p:Place {id: 'plc_koufu'})
CREATE (f:Fact {id: 'fact_freq_plc_koufu', kind: 'frequent_place', source: 'inferred',
                status: 'active', confidence: 0.6, support_days: 3,
                first_date: '2026-10-19', last_date: '2026-10-21',
                usual_arrival_min: 695, dows: [0, 1, 2], updated_ms: 1793000000000})
CREATE (f)-[:ABOUT]->(p)
CREATE (f)-[:SUPPORTED_BY]->(a), (f)-[:SUPPORTED_BY]->(b), (f)-[:SUPPORTED_BY]->(c);

MATCH (v:Visit {id: 'vis_wed'})
CREATE (n:Note {id: 'note_noodles', text: 'I really like the noodles at Koufu',
                ts_ms: 1792987500000, local_date: '2026-10-21', source: 'voice', kind: 'note'})
CREATE (n)-[:DURING]->(v)
CREATE (f:Fact {id: 'fact_pref_noodles', kind: 'preference', source: 'user', status: 'active',
                statement: 'I really like the noodles at Koufu', positive: true,
                confidence: 0.98, updated_ms: 1792987500000})
CREATE (f)-[:SUPPORTED_BY]->(n);

CREATE (:Note {id: 'note_milk', text: 'remind me to buy milk at the supermarket',
               ts_ms: 1792990000000, local_date: '2026-10-21', source: 'voice',
               kind: 'reminder', status: 'open', trigger_place_type: 'supermarket'});


// -----------------------------------------------------------------------------
// 3. COMMON QUERIES  (parameters shown inline; store.py uses $params)
// -----------------------------------------------------------------------------

// Where did I go on a given day?
MATCH (v:Visit {local_date: '2026-10-21'})
OPTIONAL MATCH (v)-[:AT]->(p:Place)
RETURN v.start_ms, v.end_ms, v.kind, v.place_type, v.activity, p.name
ORDER BY v.start_ms;

// Have I been here before? (nearest place within 75 m)
MATCH (p:Place)
WHERE distance(p.geo, point({latitude: 1.3468, longitude: 103.6801})) < 75
MATCH (p)<-[:AT]-(v:Visit)
RETURN p.name, count(v) AS visits, max(v.start_ms) AS last_visit_ms;

// When did I last see a laptop?
MATCH (v:Visit)-[:SAW]->(:Tag {name: 'laptop'})
OPTIONAL MATCH (v)-[:AT]->(p:Place)
RETURN v.start_ms, v.place_type, p.name
ORDER BY v.start_ms DESC LIMIT 1;

// What do I believe? (active facts)
MATCH (f:Fact {status: 'active'})
OPTIONAL MATCH (f)-[:ABOUT]->(p:Place)
RETURN f.kind, f.source, f.confidence, f.support_days, f.statement, p.name
ORDER BY f.confidence DESC;

// Why do I believe it? (evidence behind a fact)
MATCH (f:Fact {id: 'fact_freq_plc_koufu'})-[:SUPPORTED_BY]->(e)
RETURN labels(e)[0] AS evidence, e.local_date, e.text
ORDER BY e.local_date;

// What usually happens next after eating?
MATCH (a:Visit {activity: 'eating'})-[:NEXT]->(b:Visit)
RETURN b.place_type, b.activity, count(DISTINCT b.local_date) AS days
ORDER BY days DESC;

// Reminders that apply at a supermarket
MATCH (n:Note {kind: 'reminder', status: 'open', trigger_place_type: 'supermarket'})
RETURN n.text, n.ts_ms;


// -----------------------------------------------------------------------------
// 4. ACTIVE AGENT  (additive; written only through agent/memory_tools.py)
// -----------------------------------------------------------------------------
//
// (:Commitment)  something the user plans to do at a time
//   id                 STRING    'cmt_<hash(title, start)>'  (same plan -> same node)
//   title              STRING    e.g. 'Meet Alex', 'Dentist appointment'
//   start_ms           INT
//   end_ms?            INT       absent when unknown: a duration is never assumed
//   local_date         STRING
//   status             STRING    'tentative' (user said it) | 'confirmed' | 'cancelled'
//   source             STRING    'user_speech' | 'user_command' | 'user_answer' | 'app'
//   people             [STRING]  first names as heard (no identity resolution)
//   place_text?        STRING    'library' (free text, not linked to a Place)
//   time_ambiguous     BOOL      e.g. 'at six' with am/pm unresolved
//   evidence_segment?  STRING    audio segment id
//   prev_start_ms?, prev_end_ms?  INT   kept when the user confirms a time change
//   created_ms, updated_ms INT
//
// (:Question)  a clarification the agent (source 'agent') or the Dreamer ('dreamer') wants answered
//   id                 STRING    'qst_<hash(dedup_key)>'  (never asked twice)
//   kind               STRING    confirm_commitment | clarify_time | confirm_correction |
//                                complete_reminder | dreamer
//   text               STRING    what the user is asked
//   status             STRING    'pending' -> 'asked' -> 'answered' | 'expired'
//   priority           STRING    low | normal | high
//   created_ms, expires_ms INT;  not_before_ms?, asked_ms?, answered_ms? INT;  answer? STRING
//   payload            STRING    JSON: the allow-listed write to run if the answer is yes
//   dedup_key          STRING
//
// (:AgentLog)  one per non-trivial decision (audit; the Dreamer may read it)
//   id, ts_ms, action, priority, status ('done'|'suppressed'|'failed'|'deferred'), trigger,
//   reason, message?, dedup_key?, related [STRING], confidence
//   Never stores raw transcripts.
//
//   (:Commitment)-[:SUPPORTED_BY]->(:Note)     the transcript ingestion stored
//   (:Question)-[:ABOUT]->(:Commitment|:Note)
//   (:AgentLog)-[:CONCERNS]->(:Commitment|:Note|:Question|:Place|:Fact)
//
CREATE INDEX FOR (n:Commitment) ON (n.id);
CREATE INDEX FOR (n:Commitment) ON (n.start_ms);
CREATE INDEX FOR (n:Question)   ON (n.id);
CREATE INDEX FOR (n:Question)   ON (n.status);
CREATE INDEX FOR (n:AgentLog)   ON (n.id);
CREATE INDEX FOR (n:AgentLog)   ON (n.dedup_key);
