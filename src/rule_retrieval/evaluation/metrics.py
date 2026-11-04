from __future__ import annotations
from collections import Counter
from math import log2


def ratio(a,b):
    return a/b if b else None


def scores(predictions:list[str],references:set[str],ks:list[int]) -> dict:
    ranked=list(dict.fromkeys(predictions))
    predicted=set(ranked)
    tp=len(predicted&references)
    precision=tp/len(predicted) if predicted else 0.0
    recall=tp/len(references) if references else 0.0
    result={"tp":tp,"fp":len(predicted-references),"fn":len(references-predicted),"precision":precision,"recall":recall,
            "f1":2*precision*recall/(precision+recall) if precision+recall else 0.0,"exact_set_match":predicted==references}
    for k in ks:
        top=ranked[:k]
        result[f"recall@{k}"]=len(set(top)&references)/len(references) if references else None
        result[f"mrr@{k}"]=next((1/(i+1) for i,p in enumerate(top) if p in references),0.0)
        dcg=sum(1/log2(i+2) for i,p in enumerate(top) if p in references)
        ideal=sum(1/log2(i+2) for i in range(min(k,len(references))))
        result[f"ndcg@{k}"]=ratio(dcg,ideal)
    return result


def aggregate(issues:list[dict]) -> dict:
    if not issues:
        return {"issue_count":0,"micro":None,"macro":None,"reason":"No eligible issues; metrics undefined"}
    tp=sum(i["tp"] for i in issues)
    fp=sum(i["fp"] for i in issues)
    fn=sum(i["fn"] for i in issues)
    p=tp/(tp+fp) if tp+fp else 0.0
    r=tp/(tp+fn) if tp+fn else 0.0
    keys=[k for k,v in issues[0].items() if isinstance(v,(int,float,bool)) and k not in {"tp","fp","fn"}]
    return {"issue_count":len(issues),"micro":{"tp":tp,"fp":fp,"fn":fn,"precision":p,"recall":r,"f1":2*p*r/(p+r) if p+r else 0.0},
            "macro":{k:sum(i[k] for i in issues if i.get(k) is not None)/sum(i.get(k) is not None for i in issues) for k in keys}}
