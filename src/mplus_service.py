"""Collection and weekly publishing in the existing greyBot raid Lambda."""
import os
import mplus

import discord
import mplus_collect
import mplus_presentation
import mplus_store
import mplus_records
import store


def handle(event, cfg, now, context=None):
    if os.environ.get("MPLUS_ENABLED") != "1":
        return {"ok":True,"skipped":"mplus_disabled"}
    repo = mplus_store.Repository(store.ddb, store.TABLE, cfg["discord_guild_id"])
    if event['mode']=='mplus_records':
        channel=os.environ.get('MPLUS_CHANNEL_ID','')
        if not channel.isdecimal():raise ValueError('Mythic+ destination must be configured')
        budget=min(35,max(0,context.get_remaining_time_in_millis()/1000-15)) if context else 35
        return {'ok':True,**mplus_records.process(repo,cfg,channel,now,budget=budget)}
    if event["mode"] == "mplus_collect":
        budget = min(35, max(0, context.get_remaining_time_in_millis()/1000-15)) if context else 35
        return {"ok":True, **mplus_collect.collect(repo,cfg,now,budget=budget)}
    summary = mplus_collect.weekly_data(repo,now)
    if event.get("dry"):
        return {"ok":True,"summary":summary}
    # Do not publish an apparently empty week during a collection outage.
    if not summary.get("collector_at") or (now-mplus.stamp(summary["collector_at"])).total_seconds() > 3600:
        raise RuntimeError("Mythic+ collection is stale; restore collection before publishing")
    if not summary.get('season'):
        raise RuntimeError('Mythic+ season metadata is missing; cannot label the recap week')
    # Explicit policy prevents a deployment from silently selecting a score definition.
    if os.environ.get("MPLUS_SCORE_POLICY") != "overall_for_participants":
        return {"ok":True,"skipped":"score_policy_required"}
    channel = os.environ.get("MPLUS_CHANNEL_ID", "")
    if not channel.isdecimal() or not cfg.get("recap_page_url") or not cfg.get("recap_page_bucket"):
        raise ValueError("Mythic+ destination and recap website must be configured")
    key = summary["end"][:10]
    previous = repo.get("POST#"+key)
    if previous:
        return {"ok":True,"skipped":"already_claimed","state":previous["state"]}
    path = "mplus/"+key
    base = cfg["recap_page_url"].rstrip("/")
    page_url, image_url = base+"/"+path+"/?format=io-v2", base+"/"+path+"/card.png"
    page = mplus_presentation.page(summary,cfg["guild_name"])
    image = mplus_presentation.card(summary,cfg["guild_name"])
    if not image:
        raise RuntimeError("Mythic+ card rendering failed; do not publish a broken card")
    from handler import publish_bytes
    # Claim before public writes so retries cannot replace an already posted report.
    if not repo.put("POST#"+key,{"state":"preparing","at":now.isoformat()},once=True):
        return {"ok":True,"skipped":"already_claimed"}
    try:
        publish_bytes(cfg,path+"/index.html",page.encode(),"text/html; charset=utf-8",cache='no-cache, max-age=0, must-revalidate')
        publish_bytes(cfg,path+"/card.png",image,"image/png")
        repo.put("POST#"+key,{"state":"sending","at":now.isoformat(),"url":page_url})
        result=discord.post_to({"bot_token":cfg["bot_token"],"channel":channel},
            mplus_presentation.discord_post(summary,cfg["guild_name"],page_url,image_url),max_attempts=1)
        repo.put("POST#"+key,{"state":"posted","at":now.isoformat(),"url":page_url,
                            "message":str(getattr(result,"message_id", ""))})
    except Exception:
        # A timeout may follow Discord accepting the post; never retry that blindly.
        repo.put("POST#"+key,{"state":"needs_review","at":now.isoformat(),"url":page_url})
        raise
    return {"ok":True,"posted":True,"url":page_url}
