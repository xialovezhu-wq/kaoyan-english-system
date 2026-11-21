"""Find candidate point IDs by date/topic, disclose observations only via the active memory reader."""

from contextlib import closing

from .errors import ValidationError
from .memory_query import query_memory_context
from .memory_store import open_memory_projection


def history_page(repo, state, *, day=None, source=None, search="", offset=0, limit=8,
                 generation=None, point_key=None):
    if not 1 <= limit <= 50 or offset < 0 or len(search) > 200:
        raise ValidationError("invalid history page bounds")
    if offset and not generation:
        raise ValidationError("continuing history requires the generation from the preceding page")
    if day:
        from .evening_feedback import valid_date

        valid_date(day)
    db, manifest = open_memory_projection(state)
    with closing(db):
        version = manifest["generation"]
        if generation and generation != version:
            raise ValidationError("the history generation changed; restart at the first page")
        where = ["p.answer_safe=1", "p.visibility='answer_free'"]
        args = []
        if search:
            where.append("""(instr(lower(p.point_key),lower(?))>0 OR instr(
                lower(coalesce(json_extract(e.episode_json,'$.first_attempt'),'') || ' ' ||
                coalesce(json_extract(e.episode_json,'$.correction'),'') || ' ' ||
                coalesce(json_extract(e.episode_json,'$.next_action'),'')),lower(?))>0)""")
            args.extend([search, search])
        if day:
            where.append("json_extract(e.episode_json,'$.study_date')=?")
            args.append(day)
        if source:
            where.append("EXISTS (SELECT 1 FROM json_each(e.source_ids_json) s WHERE s.value=?)")
            args.append(source)
        # Normal offsets count points. Only a continuation for one oversized point
        # pages its matching episode IDs, through the same authorized reader.
        columns = "p.point_key, NULL"
        order = "p.point_key"
        if point_key is not None:
            where.append("p.point_key=?")
            args.append(point_key)
            columns = "p.point_key, e.episode_id"
            order = "p.point_key, e.episode_id"
        query = (
            f"SELECT DISTINCT {columns} FROM points p JOIN episodes e ON e.point_key=p.point_key WHERE "
            + " AND ".join(where)
            + f" ORDER BY {order} LIMIT ? OFFSET ?"
        )
        candidates = db.execute(query, (*args, limit + 1, offset)).fetchall()
    points = {}
    withheld = 0
    incomplete = []
    for key, episode_id in candidates[:limit]:
        result = query_memory_context(
            repo, state, keys=[key], ids=[episode_id] if episode_id else [],
            view="history", source_id=source, max_bytes=16384
        )
        if result.get("generation") != version:
            raise ValidationError("the active history generation changed during the read")
        for p in result["matched"]:
            if p["key"] != key:
                continue
            episodes = [e for e in p.get("episodes", []) if (not day or e.get("study_date") == day)
                        and (episode_id is None or e["episode_id"] == episode_id)]
            if search and search.casefold() not in p["key"].casefold():
                episodes = [
                    e
                    for e in episodes
                    if search.casefold()
                    in " ".join(
                        str(e.get(k) or "") for k in ("first_attempt", "correction", "next_action")
                    ).casefold()
                ]
            if episodes:
                point = points.setdefault(key, dict(key=key, episodes=[], sources=[], summary_sha256=p["summary_sha256"]))
                point["episodes"].extend(episodes)
                refs = {ref for e in episodes for ref in e["source_keys"]}
                for ref in p.get("sources", []):
                    if "references" in ref:
                        kept = [r for r in ref["references"] if r["source_key"] in refs]
                        if not kept:
                            continue
                        ref = {**ref, "references": kept}
                    elif ref.get("source_key") not in refs:
                        continue
                    if ref not in point["sources"]:
                        point["sources"].append(ref)
        coverage = result["coverage"]
        if coverage.get("episodes_omitted") or coverage.get("points_omitted"):
            detail = {"reason": "observation_exceeds_budget" if episode_id else "point_exceeds_budget"}
            if not episode_id:
                detail["continuation"] = dict(point_key=key, generation=version, offset=0,
                                              study_date=day, source_id=source, search=search)
            else:
                detail.update(point_key=key, episode_id=episode_id, generation=version)
            incomplete.append(detail)
        withheld += sum(len(result.get(k, [])) for k in ("unavailable", "pending", "conflicts"))
    return dict(
        generation=version,
        points=list(points.values()),
        withheld=withheld,
        incomplete=incomplete,
        next_offset=offset + limit if len(candidates) > limit else None,
        formal_write_count=0,
    )
