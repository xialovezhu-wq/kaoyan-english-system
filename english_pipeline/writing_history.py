"""Read-only fixed-cohort checks. Retrieval, context inclusion and version identity are distinct."""
from .memory_query import query_memory_context
from .memory_store import open_memory_projection
from .util import load_json,object_sha256
from .web_review import package_root
from .writing_sources import digest_message
from .memory_store import search_tokens
from .errors import PipelineError
from .util import atomic_write_json
import re

CATALOG = "writing-view/first-submission-locators.json"


def publish_locators(repo,state,manifest,dates,evidence):
    """Index one verified original per attempt from existing formal rows. Exclude unreviewed work."""
    attempts={}
    for records in dates.values():
        for ep,point in records:
            ident=ep["episode_id"]; row=evidence.get(ident)
            if (not row or ep.get("writing_stage")!="first_draft" or row["record_sha256"]!=ep.get("formal_event_sha256")):
                continue
            w=row["event"]["writing_memory"]
            ordering=(ep["observed_at"],ident)
            if w["attempt_id"] not in attempts or ordering<attempts[w["attempt_id"]][0]:
                attempts[w["attempt_id"]]=(ordering,row,point)
    members=[]; unavailable=0
    for order,row,point in attempts.values():
        w=row["event"]["writing_memory"]
        ref=next(r for r in row["evidence_refs"] if r["kind"]=="user_message")
        try:
            root=package_root(state,repo,ref["package_id"])
            original=load_json(root/"source.json")["identity"]["writing_session"]
            if (original["attempt_id"]!=w["attempt_id"] or load_json(root/"receipt.json")["package_sha256"]!=ref["package_sha256"]):
                unavailable+=1;continue
        except (OSError,KeyError,ValueError,PipelineError):
            unavailable+=1;continue
        task=original["task"]
        terms=search_tokens(task["prompt"]+" "+re.sub(r"[-_]+"," ",task["task_id"]))
        members.append(dict(episode_id=row["event_id"],point_key=point,terms=terms,attempt_id=w["attempt_id"]))
    payload=dict(generation=manifest["generation"],members=members,unavailable=unavailable)
    atomic_write_json(state/CATALOG,dict(payload,sha256=object_sha256(payload)))


def first_draft_candidates(state,generation,query):
    """Find task locators only on explicit original-draft history requests; native memory enforces scope afterwards."""
    path=state/CATALOG
    if not re.search(r"首稿|原稿|初稿|first draft|initial submission",query,re.I) or not path.is_file():
        return []
    catalog=load_json(path)
    if catalog.get("generation")!=generation or catalog.get("sha256")!=object_sha256({k:v for k,v in catalog.items() if k!="sha256"}):
        return []
    terms=set(search_tokens(query))
    # General exam vocabulary gives short Chinese comparison terms an English task-label lookup.
    for labels,words in ((("排名","排序","名次"),("ranking","rank")), (("倍数","几倍"),("multiple","multiples")),
                         (("趋势","走势"),("trend",)), (("占比","份额","比例"),("share","proportion")),
                         (("邀请","请柬"),("invitation","invite")), (("建议","建议信"),("advice","suggestion")),
                         (("申请",),("application",)), (("道歉",),("apology",)), (("投诉",),("complaint",))):
        if any(label in query for label in labels):terms.update(words)
    rows=catalog["members"]
    document_counts={term:sum(term in r["terms"] for r in rows) for term in terms}
    ranked=[]
    for r in rows:
        score=sum(len(term)/(1+document_counts[term]) for term in terms.intersection(r["terms"]))
        if score:ranked.append((-score,r["episode_id"],r["point_key"]))
    return [(point,ident) for _,ident,point in sorted(ranked)[:5]]



def evaluate(repo,state,cohort):
    db,manifest=open_memory_projection(state)
    result=[]
    try:
        for sample in cohort["samples"]:
            target=sample["event_id"]
            root=package_root(state,repo,sample["package"])
            file=load_json(root/"conversation.json")
            index=int(sample["pointer"].split("/")[-1])
            m=file["messages"][index]
            identity=load_json(root/"source.json")["identity"]["writing_session"]
            source_ok=(m["role"]=="user" and m["message_id"]==sample["message_id"] and
                digest_message(m)==sample["source_sha256"] and identity["attempt_id"]==sample["attempt_id"] and
                identity["task"]["task_id"]==sample["task_id"] and identity["session_id"]==sample["session_id"] and
                load_json(root/"receipt.json")["package_sha256"]==sample["package_sha256"])
            options=dict(view=cohort["view"],max_bytes=cohort["max_bytes"])
            exact=query_memory_context(repo,state,ids=[target],**options)
            natural=query_memory_context(repo,state,query=sample["query"],**options)
            def episodes(point):
                return point.get("episodes") or ([point["latest_observation"]] if point.get("latest_observation") else [])
            ranked=[e for p in natural["matched"] for e in episodes(p)]
            hit=next((i+1 for i,e in enumerate(ranked[:cohort["k"]]) if e["episode_id"]==target),None)
            exact_ok=any(e["episode_id"]==target and e.get("attempt_id")==sample["attempt_id"]
                         and e.get("writing_stage")=="first_draft" for p in exact["matched"] for e in episodes(p))
            indexed=bool(db.execute("SELECT 1 FROM episodes WHERE point_key=? AND episode_id=?",
                                    (sample["point_key"],target)).fetchone())
            result.append(dict(event_id=target,original_verified=source_ok,indexed=indexed,exact_version=exact_ok,
                natural_rank=hit,returned_episodes=len(ranked),returned_points=len(natural["matched"]),
                natural_candidate_point=any(p["key"]==sample["point_key"] for p in natural["matched"]),
                context_included=any(e["episode_id"]==target for e in ranked),context_coverage=natural.get("coverage",{}),
                original_export_scope=sample["scope"]))
    finally:
        db.close()
    return dict(cohort_id=cohort["cohort_id"],cohort_sha256=object_sha256(cohort),generation=manifest["generation"],
        k=cohort["k"],budget=cohort["max_bytes"],n=len(result),exact=sum(r["exact_version"] for r in result),
        originals=sum(r["original_verified"] for r in result),natural=sum(r["natural_rank"] is not None for r in result),
        cases=result,formal_write_count=0)
