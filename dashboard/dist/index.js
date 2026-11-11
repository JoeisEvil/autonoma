(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useRef = SDK.hooks.useRef;
  var useCallback = SDK.hooks.useCallback;
  var h = React.createElement;
  var API = "/api/plugins/autonoma";
  var POLL_MS = 2500;

  var LABELS = {
    "system": "System changes",
    "spend": "Spending",
    "fs.delete": "Delete files",
    "git.remote": "Push & publish",
    "net.outbound": "Network requests",
    "msg.send": "Send messages",
    "fs.write": "Edit files",
    "git.local": "Local git",
    "shell.exec": "Run commands",
    "read": "Read"
  };

  var OUTCOMES = {
    auto: "Ran on its own",
    approved: "You approved",
    allowed: "Pre-approved",
    stopped: "Stopped",
    error: "Failed",
    read: "Read"
  };

  var REASONS = {
    clean_call: "clean action",
    clean_turn: "clean turn",
    approved_once: "you approved",
    approved_session: "you approved (session)",
    approved_always: "you approved always",
    denied: "you said no",
    timeout: "no answer",
    corrected: "you corrected it",
    tool_error: "tool failed",
    decay: "unused",
    operator_set: "set by you",
    reset: "reset"
  };

  var INCIDENT_GLYPH = {
    workaround: "↪",
    retry: "⟳",
    outside_content: "⚠",
    self_edit: "✎",
    denied: "✕",
    correction: "↺",
    autonomy: "▲",
    demoted: "▼"
  };

  function ago(ts) {
    var d = Date.now() / 1000 - ts;
    if (d < 5) return "now";
    if (d < 60) return Math.floor(d) + "s ago";
    if (d < 3600) return Math.floor(d / 60) + "m ago";
    if (d < 86400) return Math.floor(d / 3600) + "h ago";
    return Math.floor(d / 86400) + "d ago";
  }

  function clock(ts) {
    var d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function cx() {
    return Array.prototype.filter.call(arguments, Boolean).join(" ");
  }

  function post(path, body) {
    return SDK.fetchJSON(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  function Spark(props) {
    var pts = props.points || [];
    if (pts.length < 2) pts = [pts[0] || 0, pts[0] || 0];
    var w = 120, hgt = 28;
    var step = w / (pts.length - 1);
    var y = function (v) { return hgt - (Math.max(0, Math.min(100, v)) / 100) * hgt; };
    var d = pts.map(function (v, i) { return (i ? "L" : "M") + (i * step).toFixed(1) + " " + y(v).toFixed(1); }).join(" ");
    var area = d + " L" + w + " " + hgt + " L0 " + hgt + " Z";
    return h("svg", { className: "an-spark", viewBox: "0 0 " + w + " " + hgt, preserveAspectRatio: "none", "aria-hidden": true },
      h("line", { x1: 0, x2: w, y1: y(props.threshold), y2: y(props.threshold), className: "an-spark-th" }),
      h("path", { d: area, className: "an-spark-area" }),
      h("path", { d: d, className: "an-spark-line" }),
      h("circle", { cx: w, cy: y(pts[pts.length - 1]), r: 2.2, className: "an-spark-dot" })
    );
  }

  function Meter(props) {
    var c = props.c;
    return h("div", { className: "an-meter", role: "meter", "aria-valuenow": c.score, "aria-valuemin": 0, "aria-valuemax": 100,
                      "aria-label": c["class"] + " trust" },
      h("div", { className: "an-meter-fill", style: { width: Math.max(0, Math.min(100, c.score)) + "%" } }),
      h("div", { className: "an-meter-th", style: { left: c.threshold + "%" }, title: "Threshold " + c.threshold })
    );
  }

  function statusOf(c, frozen) {
    if (c.in_timeout) return { key: "timeout", text: "Timeout · " + c.turns_left + " turns" };
    if (c.autonomous) return { key: "auto", text: "Autonomous" };
    return { key: "asks", text: "Asks first" };
  }

  function TrustRow(props) {
    var c = props.c;
    var prev = useRef(c.score);
    var _f = useState(null), flash = _f[0], setFlash = _f[1];
    useEffect(function () {
      if (c.score !== prev.current) {
        setFlash(c.score > prev.current ? "up" : "down");
        prev.current = c.score;
        var t = setTimeout(function () { setFlash(null); }, 1400);
        return function () { clearTimeout(t); };
      }
    }, [c.score]);
    var st = statusOf(c, props.frozen);
    var last = c.last_event;
    return h("div", { className: cx("an-row", "is-" + st.key, flash && "flash-" + flash) },
      h("div", { className: "an-row-name" },
        h("span", { className: "an-row-label" }, LABELS[c["class"]] || c["class"]),
        h("code", { className: "an-row-class" }, c["class"])
      ),
      h("div", { className: "an-row-meter" },
        h(Meter, { c: c }),
        h("div", { className: "an-row-sub" },
          last ? h("span", { className: cx("an-delta", last.delta > 0 ? "pos" : last.delta < 0 ? "neg" : "zero") },
            (last.delta === 0 ? "±0" : (last.delta > 0 ? "+" : "") + last.delta.toFixed(1)) + " · " + (REASONS[last.reason] || last.reason.replace(/_/g, " "))) : h("span", { className: "an-muted" }, "no activity yet"),
          !c.autonomous && !c.in_timeout ? h("span", { className: "an-muted" }, "~" + c.calls_to_autonomy + " clean to go") : null
        )
      ),
      h("div", { className: "an-row-score" },
        h("span", { className: "an-score" }, Math.round(c.score)),
        h("span", { className: "an-score-th" }, "/" + c.threshold)
      ),
      h(Spark, { points: c.history, threshold: c.threshold }),
      h("span", { className: cx("an-chip", "chip-" + st.key) }, st.text),
      h("button", { className: "an-icon-btn", title: "Reset " + c["class"] + " to the starting score",
                    onClick: function () { props.onReset(c["class"]); } }, "↺")
    );
  }

  function Stat(props) {
    return h("div", { className: cx("an-stat", props.tone && "tone-" + props.tone) },
      h("div", { className: "an-stat-value" }, props.value),
      h("div", { className: "an-stat-label" }, props.label),
      props.sub ? h("div", { className: "an-stat-sub" }, props.sub) : null
    );
  }

  function Incident(props) {
    var i = props.i;
    return h("li", { className: cx("an-inc", "inc-" + i.kind, props.fresh && "is-fresh") },
      h("span", { className: "an-inc-glyph", "aria-hidden": true }, INCIDENT_GLYPH[i.kind] || "•"),
      h("div", { className: "an-inc-body" },
        h("div", { className: "an-inc-title" }, i.title,
          i.outcome && i.outcome !== "read" ? h("span", { className: cx("an-chip", "small", "out-" + i.outcome) }, OUTCOMES[i.outcome]) : null),
        h("code", { className: "an-inc-detail" }, i.detail)
      ),
      h("time", { className: "an-inc-time", title: new Date(i.ts * 1000).toLocaleString() }, ago(i.ts))
    );
  }

  var FILTERS = [
    ["actions", "Actions"],
    ["stopped", "Stopped"],
    ["approved", "Approved"],
    ["auto", "On its own"],
    ["all", "Everything"]
  ];

  function matches(e, f) {
    if (f === "all") return true;
    if (f === "actions") return e.outcome !== "read";
    if (f === "approved") return e.outcome === "approved" || e.outcome === "allowed";
    return e.outcome === f;
  }

  function Activity(props) {
    var _f = useState("actions"), filter = _f[0], setFilter = _f[1];
    var rows = props.events.filter(function (e) { return matches(e, filter); }).slice(0, 40);
    return h("section", { className: "an-card an-activity" },
      h("header", { className: "an-card-head" },
        h("h3", null, "Activity"),
        h("div", { className: "an-filters", role: "tablist" },
          FILTERS.map(function (f) {
            return h("button", { key: f[0], role: "tab", "aria-selected": filter === f[0],
                                 className: cx("an-filter", filter === f[0] && "on"),
                                 onClick: function () { setFilter(f[0]); } }, f[1]);
          })
        )
      ),
      rows.length === 0 ? h("p", { className: "an-empty" }, "Nothing here yet.") :
        h("ol", { className: "an-log" },
          rows.map(function (e) {
            var taint = e.flags && e.flags.indexOf("taint:") === 0;
            return h("li", { key: e.id, className: cx("an-log-row", "out-" + e.outcome, props.fresh[e.id] && "is-fresh") },
              h("time", { className: "an-log-time" }, clock(e.ts)),
              h("span", { className: cx("an-chip", "small", "out-" + e.outcome) }, OUTCOMES[e.outcome]),
              h("span", { className: "an-log-class" }, LABELS[e.action_class] || e.action_class),
              h("code", { className: "an-log-summary", title: e.tool }, e.args_summary || e.tool),
              taint ? h("span", { className: "an-chip small chip-warn", title: "After reading " + e.flags.slice(6) }, "outside content") : null,
              e.outcome === "error" && e.error ? h("span", { className: "an-log-err", title: e.error }, e.error) : null
            );
          })
        )
    );
  }

  function Handoff(props) {
    var o = props.h;
    return h("div", { className: "an-handoff" },
      h("span", { className: "an-handoff-tag" }, "Unfinished session"),
      h("span", { className: "an-handoff-task" }, o.task || "(no task text)"),
      h("span", { className: "an-muted" }, o.actions + " actions · last active " + ago(o.last_activity)),
      h("span", { className: "an-muted" }, "The next session starts with a handoff note."),
      h("button", { className: "an-btn ghost", onClick: function () { props.onClear(o.session_id); } }, "Dismiss")
    );
  }

  function AutonomaPage() {
    var _s = useState(null), data = _s[0], setData = _s[1];
    var _e = useState(null), error = _e[0], setError = _e[1];
    var _w = useState(24), win = _w[0], setWin = _w[1];
    var _b = useState(false), busy = _b[0], setBusy = _b[1];
    var _fr = useState({}), fresh = _fr[0], setFresh = _fr[1];
    var seen = useRef(null);
    var seenInc = useRef(null);
    var _fi = useState({}), freshInc = _fi[0], setFreshInc = _fi[1];

    var load = useCallback(function () {
      return SDK.fetchJSON(API + "/state?window_h=" + win).then(function (d) {
        var ids = {}, newIds = {};
        d.events.forEach(function (e) { ids[e.id] = true; if (seen.current && !seen.current[e.id]) newIds[e.id] = true; });
        seen.current = ids;
        var incKeys = {}, newInc = {};
        d.incidents.forEach(function (i) { var k = i.kind + i.ts; incKeys[k] = true; if (seenInc.current && !seenInc.current[k]) newInc[k] = true; });
        seenInc.current = incKeys;
        if (Object.keys(newIds).length) setFresh(newIds);
        if (Object.keys(newInc).length) setFreshInc(newInc);
        setData(d);
        setError(null);
      }).catch(function (err) { setError(String((err && err.message) || err)); });
    }, [win]);

    useEffect(function () {
      seen.current = null;
      seenInc.current = null;
      load();
      var id = setInterval(function () { if (!document.hidden) load(); }, POLL_MS);
      return function () { clearInterval(id); };
    }, [load]);

    function act(body) {
      setBusy(true);
      return post("/trust", body).then(load).catch(function (err) { setError(String((err && err.message) || err)); })
        .then(function () { setBusy(false); });
    }

    if (!data) {
      return h("div", { className: "autonoma" },
        error ? h("div", { className: "an-error" }, "Couldn't load Autonoma: " + error +
          ". Is the plugin enabled for this profile?") : h("div", { className: "an-loading" }, "Loading trust ledger…"));
    }

    var s = data.stats;
    var autonomous = data.classes.filter(function (c) { return c.autonomous; }).length;
    return h("div", { className: "autonoma" },
      h("header", { className: "an-head" },
        h("div", null,
          h("h2", { className: "an-title" }, "Autonoma"),
          h("p", { className: "an-subtitle" },
            "Earned autonomy for ", h("strong", null, data.profile), ". ",
            autonomous + " of " + data.classes.length + " action types run without asking.")
        ),
        h("div", { className: "an-head-tools" },
          h("span", { className: cx("an-live", error && "is-off") }, h("i", null), error ? "Offline" : "Live"),
          h("div", { className: "an-seg" },
            [[24, "24h"], [168, "7d"], [720, "30d"]].map(function (o) {
              return h("button", { key: o[0], className: cx(win === o[0] && "on"), onClick: function () { setWin(o[0]); } }, o[1]);
            })
          ),
          h("button", { className: cx("an-btn", data.frozen && "on"), disabled: busy,
                        title: "Freeze stops scores changing; the gate still applies",
                        onClick: function () { act({ op: data.frozen ? "unfreeze" : "freeze" }); } },
            data.frozen ? "Frozen · unfreeze" : "Freeze scores")
        )
      ),
      !data.enabled ? h("div", { className: "an-banner" }, "The trust gate is off (enabled: false). Actions are logged but never asked about.") : null,
      data.handoff ? h(Handoff, { h: data.handoff, onClear: function (sid) { post("/handoff/clear", { session_id: sid }).then(load); } }) : null,
      h("div", { className: "an-stats" },
        h(Stat, { value: s.autonomy_pct + "%", label: "Ran on its own", sub: s.auto + " of " + s.actions + " actions", tone: "accent" }),
        h(Stat, { value: s.approved, label: "You approved" }),
        h(Stat, { value: s.stopped, label: "Stopped", sub: "denied, timed out or no one there", tone: s.stopped ? "warn" : null }),
        h(Stat, { value: data.incidents.filter(function (i) { return i.kind === "workaround" || i.kind === "retry" || i.kind === "outside_content" || i.kind === "self_edit"; }).length,
                  label: "Incidents", sub: "workarounds, retries, outside content, self-edits", tone: "danger" })
      ),
      h("div", { className: "an-grid" },
        h("section", { className: "an-card an-trust" },
          h("header", { className: "an-card-head" },
            h("h3", null, "Trust"),
            h("span", { className: "an-muted" }, "Below the line it asks you first. Clean work raises it; denials and corrections drop it.")
          ),
          h("div", { className: "an-rows" },
            data.classes.map(function (c) {
              return h(TrustRow, { key: c["class"], c: c, frozen: data.frozen,
                                   onReset: function (cls) { act({ op: "reset", "class": cls }); } });
            })
          )
        ),
        h("section", { className: "an-card an-incidents" },
          h("header", { className: "an-card-head" },
            h("h3", null, "Incidents"),
            h("span", { className: "an-muted" }, "Moments worth a look")
          ),
          data.incidents.length === 0 ? h("p", { className: "an-empty" }, "Nothing notable yet.") :
            h("ul", { className: "an-inc-list" },
              data.incidents.map(function (i) { return h(Incident, { key: i.kind + i.ts, i: i, fresh: freshInc[i.kind + i.ts] }); }))
        )
      ),
      h(Activity, { events: data.events, fresh: fresh }),
      error ? h("div", { className: "an-error small" }, error) : null
    );
  }

  window.__HERMES_PLUGINS__.register("autonoma", AutonomaPage);
})();
