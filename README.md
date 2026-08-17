# mcp-cloakroom

**Senate roll call votes over the [Model Context Protocol](https://modelcontextprotocol.io).**
Every recorded Senate vote from 1789 to the present, queryable by an AI assistant
or any MCP client.

The federal government publishes no Senate roll call vote API. The Congress.gov
API ships House votes and has no Senate equivalent, and the govinfo bulk data
collections contain no vote collection at all. **senate.gov XML is the only
official machine-readable source**, and it covers the 101st Congress forward.
For the full historical record, **Voteview is the only bulk archive**, reaching
back to 1789.

Third-party APIs do exist. LegiScan, for one, serves Senate roll calls as JSON.
What this server adds is the layer on top: **it is the only MCP server with an
ideological-analysis layer over Senate votes.** The other Senate and Congress
MCP servers wrap vote records; none of them touch Voteview's DW-NOMINATE
estimates, which is where the interesting questions live.

---

## What you can ask it

- *How did the Senate vote on S. 5271?*
- *Show me every vote Senator X cast against their party this Congress.*
- *How often do these two senators agree?*
- *Who broke ranks on this vote, and was any of it actually unpredictable?*
- *Which of this senator's votes does the model fail to explain?*
- *What Senate votes mention judicial courts?* (the first one is from 1789)
- *What is the Senate's schedule this week?*

The two questions about prediction are the reason this exists. Counting party-line breaks is
easy. Asking whether a break was *predictable* needs the ideal points, and that
is the layer no other Senate MCP server has.

---

## Run it yourself

**This is the primary way to use it.** The image is public and multi-arch
(amd64 and arm64), so a laptop, a Raspberry Pi, or a server all work. You do not
need Python, a build toolchain, or an account anywhere. There are no API keys,
because none of the upstream data sources require one.

```bash
curl -O https://raw.githubusercontent.com/pete-builds/mcp-cloakroom/main/docker-compose.yml
docker compose up -d
docker compose logs -f
```

That is the whole install. The server is then at `http://localhost:3728/mcp`.

### What happens on first start

The container downloads the published bulk data and builds a local SQLite
database before it starts serving.

| | |
|---|---|
| **Downloaded once** | about 140 MB |
| **Disk needed** | about 1.5 GB free for the database |
| **Time** | usually 3 to 8 minutes, mostly the 126 MB member-vote file |
| **Network requests** | 5 total |
| **Repeat starts** | immediate; the database lives in a Docker volume |

The load runs in the container's entrypoint, before the server starts, so
**while it is loading nothing is listening on 3728 yet**: `curl` gets a
connection refused rather than an HTTP response. Watch `docker compose logs -f`
instead, which prints progress as each file lands. The container healthcheck has
a long start period so the container is not killed mid-load.

Once the server is up, `GET /healthz` returns `200`. It returns `503` with
`"status": "loading"` if the server is running against a database that has not
been populated, which is what you get when the automatic load is turned off
(`CLOAKROOM_AUTO_INGEST=false`) and `ingest.py` has not been run yet.

Check on it any time:

```bash
curl -s http://localhost:3728/healthz
docker compose exec mcp-cloakroom python ingest.py --status
```

To refresh later with newly published data (safe to re-run, it converges):

```bash
docker compose exec mcp-cloakroom python ingest.py
```

### Running from source

```bash
git clone https://github.com/pete-builds/mcp-cloakroom
cd mcp-cloakroom
pip install -r requirements.in
python ingest.py     # one-time bulk load
python server.py
```

---

## Connect a client

MCP is an open protocol. Any MCP-compatible client works. The server speaks
**streamable HTTP** at `/mcp`. Configuration for a few common clients follows,
alphabetically.

### Claude Code

```bash
claude mcp add cloakroom --transport http --scope user --url http://localhost:3728/mcp
```

### Claude Desktop

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cloakroom": {
      "type": "http",
      "url": "http://localhost:3728/mcp"
    }
  }
}
```

### Gemini CLI

In `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "cloakroom": {
      "httpUrl": "http://localhost:3728/mcp",
      "timeout": 30000
    }
  }
}
```

Use `httpUrl`, not `url`. Gemini CLI selects its transport from which key you
provide: `httpUrl` gives streamable HTTP, while `url` selects the older SSE
transport, which this server does not serve.

### Anything else

Point your client at `http://localhost:3728/mcp` using streamable HTTP. If the
server is on another machine, substitute its address; it binds all interfaces by
default.

Optional bearer auth is available for untrusted networks: set
`MCP_AUTH_REQUIRED=true` and `MCP_AUTH_TOKEN=<token>`, then send
`Authorization: Bearer <token>`.

---

## Tools

| Tool | What it does |
|---|---|
| `list_votes` | Roll calls, newest first, filtered by congress, session, or date range |
| `get_vote` | One vote in full, with every senator's position and a party breakdown |
| `find_votes` | Search by text, bill number, question type, or result |
| `get_member_votes` | One senator's record, across a career or a single congress |
| `compare_members` | Pairwise agreement rate between two senators |
| `find_defectors` | Who voted against their party's majority position |
| `find_unexpected_votes` | Votes the DW-NOMINATE model did not predict |
| `get_schedule` | Hearings, floor calendar, current members, and the live vote index |

Every response is JSON: `{"data": ...}` on success, `{"error", "code", "details"}`
on failure, plus a `provenance` block naming the sources behind that answer.

### Two numbering schemes

Roll calls are addressable both ways, because the two publishers number them
differently and neither is wrong:

- **Voteview `rollnumber`** counts continuously across a Congress. The 119th
  reached 890.
- **senate.gov `vote_number`** restarts at 1 each session. The 119th's second
  session is at 231.

They are the same vote. `get_vote(congress=119, rollnumber=890)` and
`get_vote(congress=119, session=2, vote_number=231)` return it identically.
Votes before the 101st Congress have no `vote_number`, since senate.gov's
published roll calls begin there.

---

## The analysis layer

Two different notions of "went against expectation" are kept deliberately apart,
because collapsing them is the most common way to be confidently wrong here.

**Party defection** (`find_defectors`) is observable and model-free: the senator
voted against the position most of their party took. No estimate involved.

**A model-unexpected vote** (`find_unexpected_votes`) is probabilistic. Voteview
publishes `prob`, the estimated probability that a member cast the vote they are
recorded as casting, given the fitted model. A low value means the model did not
predict that vote, which says nothing at all about party.

The two come apart constantly, and the gap is the interesting part. A moderate
crossing party lines is often a defection the model predicts perfectly well. A
senator voting *with* their party can be the least predictable vote of the day.
Reporting only one and calling it "breaking ranks" loses that.

### What these numbers do not mean

DW-NOMINATE is probably the most misread quantity in quantitative political
science, so every analysis response carries an `interpretation` block saying so
in the payload itself, not just here:

- The dimensions are **recovered from roll call voting behaviour**. They are not
  a measure of ideology, policy positions, beliefs, intent, or quality of
  representation, and nothing in them derives from anything a legislator said.
- **"Defection" is a statistical description, not a claim about motive.** Agenda
  control, procedural strategy, pairing, local interests, and vote timing all
  produce the same signature as conviction does.
- **Ideal points are estimates with error.** Each member's `fit` block reports
  the votes behind the estimate and the model's classification error rate, so a
  poorly identified position is visible rather than implied. Small differences
  between members are usually not meaningful.
- **Only recorded roll calls exist in this data.** Everything settled by voice
  vote, unanimous consent, or never brought to the floor leaves no trace.

The tools report the measure and stop. Labels are geometric
(`toward_opposing_party`) rather than characterizations, thresholds are stated
and the underlying continuous value is always returned, and no field
editorializes about a legislator. There are tests enforcing that.

## How it gets the data

Three sources, each doing the one thing it does best.

**Voteview (UCLA)** is the historical spine. It publishes the complete
1789-present archive as bulk CSV, which is three HTTP requests for the entire
record, and it carries the DW-NOMINATE coordinates the defection analysis needs.

**unitedstates/congress-legislators** supplies identity. Its `bioguide` id is
the join key between the other two sources.

**senate.gov XML** supplies current-session freshness and the verbatim fields the
bulk data does not carry, such as the exact question text and the majority
requirement.

### About senate.gov traffic

**This project uses only published public data. It does not scrape or probe
senate.gov.**

The entire historical archive comes from Voteview's bulk files, so the volume of
work this server does bears no relation to the traffic it generates. Ongoing use
is a handful of small requests against XML feeds that are published for reuse.

The restraint is enforced in code rather than promised in documentation:

- **A fixed URL allowlist.** Requests are only ever made to a literal URL from a
  declared list, or to one of two anchored, bounds-checked path patterns. There
  is no code path that fetches an arbitrary URL, redirects are never followed,
  and nothing derives a URL from a link or a tool argument.
- **Conditional GETs.** Stored ETag and Last-Modified validators mean a repeat
  request normally costs a `304` with no body.
- **Permanent caching of closed sessions.** A past session's roll call cannot
  change, so it is fetched at most once, ever.
- **No bulk fetching.** Individual roll call files are retrieved one at a time,
  only when a caller asks for that specific vote.
- **An identifying User-Agent** carrying a contact URL.
- **A rate limit** with serialized requests, configurable and enforced.

Stack fingerprinting, port scanning, vulnerability probing, fuzzing, and any
interaction with authentication, staging, or administrative surfaces are out of
scope permanently, not behind a flag.

You can also run with no senate.gov traffic whatsoever. Set
`CLOAKROOM_SENATE_FEEDS=` and everything except `get_schedule` still works, on
the bulk archive alone.

---

## Configuration

Every setting is optional; the defaults run correctly with no configuration.
See [`.env.example`](.env.example) for the full list. There are no credentials,
and none should be added.

| Variable | Default | Purpose |
|---|---|---|
| `CLOAKROOM_DB_PATH` | `./data/cloakroom.db` | Where the store lives |
| `CLOAKROOM_AUTO_INGEST` | `true` | Load automatically when empty |
| `CLOAKROOM_SENATE_FEEDS` | all four | Which feeds are enabled; empty disables all |
| `CLOAKROOM_MIN_REQUEST_INTERVAL` | `2.0` | Seconds between senate.gov requests |
| `CLOAKROOM_REFRESH_HOURS` | `24` | Revalidation window for current-session feeds |
| `CLOAKROOM_CONTACT_URL` | this repository | Contact URL in the User-Agent |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `3728` | Bind address and port |

---

## Data sources and attribution

**Voteview** — citation required, and included in the `provenance` block of every
response:

> Lewis, Jeffrey B., Keith Poole, Howard Rosenthal, Adam Boche, Aaron Rudkin,
> and Luke Sonnet (2026). *Voteview: Congressional Roll-Call Votes Database.*
> https://voteview.com/

**unitedstates/congress-legislators** — public domain (CC0).
https://github.com/unitedstates/congress-legislators

**senate.gov** — United States Senate roll call vote XML, used as public record.
https://www.senate.gov/

This project is not affiliated with, endorsed by, or connected to the United
States Senate, UCLA, or the Voteview project.

DW-NOMINATE scores are a scholarly estimate of legislator ideal points derived
from voting behaviour. They describe voting patterns; they are not a measure of
a legislator's beliefs or quality, and the `surprise` figure this server reports
is a descriptive distance, not a statistical test.

---

## Development

```bash
pip install -r requirements-dev.in
pytest                       # tests, branch coverage, and the coverage floor
ruff check . && ruff format --check .
mypy clients tools server.py ingest.py
```

No test makes a network request; the suite runs against checked-in samples of
the real upstream files.

The tests that matter most are the ones pinning guarantees a README cannot
enforce: the URL allowlist is tested with both positive and negative controls
(including non-allowlisted URLs on senate.gov's own host, so it cannot pass by
checking the hostname), the rate limiter is asserted on real timing, the
`clerk_rollnumber` bridge is pinned against a real fixture pair, and the
`cast_code` mapping is validated by reproducing senate.gov's own published
tallies from Voteview's integers rather than by comparing a constant to itself.

## License

MIT. See [LICENSE](LICENSE).
