"""Present real cache-hit marketplace results, never cache-price substitutes."""
import math
from .a6_marketplace import MarketplaceError


def seller_result(rows,model,token_id,min_cache_hit_percent=40,min_success_percent=None,max_cache_usd=None,limit=50):
    for value,low,high,kind in [(min_cache_hit_percent,0,100,'invalid_cache_hit_percent'),(min_success_percent,0,100,'invalid_success_percent'),(max_cache_usd,0,1000,'invalid_cache_price')]:
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high):raise MarketplaceError(kind)
    result=[];excluded_cache=0;excluded_quality=0;official=None
    for row in rows:
        if row.get('model_name')!=model:continue
        hit=row.get('cache_hit_rate_24h')
        hit=hit/100 if row.get('cache_hit_state') in ('rate','zero') and isinstance(hit,(int,float)) and not isinstance(hit,bool) and 0<=hit<=10000 else None
        success=row.get('recent_success_rate')
        success=success/100 if isinstance(success,(int,float)) and not isinstance(success,bool) and 0<=success<=10000 else None
        if hit is None or hit<=min_cache_hit_percent:
            excluded_cache+=1;continue
        if min_success_percent is not None and (success is None or success<min_success_percent):
            excluded_quality+=1;continue
        cache=row.get('cache_read_price_micros')
        if max_cache_usd is not None and (not isinstance(cache,int) or cache/1e6>max_cache_usd):
            excluded_cache+=1;continue
        if official is None and isinstance(row.get('official_price'),dict):
            official={'input_micros':row['official_price'].get('input_price_micros'),'output_micros':row['official_price'].get('output_price_micros')}
        result.append({'channel_id':row['channel_id'],'listing_id':row['listing_id'],'supplier':row.get('supplier_name',''),
            'description':row.get('description',''),'input_micros':row.get('input_price_micros'),'output_micros':row.get('output_price_micros'),
            'cache_read_micros':cache,'cache_hit_percent':hit,'cache_hit_state':row.get('cache_hit_state'),'success_percent':success,
            'samples':row.get('sample_count'),'p50_ms':row.get('recent_p50_ms'),'authenticity_guaranteed':row.get('authenticity_guaranteed',False)})
    result.sort(key=lambda r:(r['input_micros'] is None,r['input_micros'] or 0,r['channel_id']))
    return {'model':model,'token_id':token_id,'min_cache_hit_percent':min_cache_hit_percent,'official':official,'matches':len(rows),
        'excluded_by_cache':excluded_cache,'excluded_by_quality':excluded_quality,'returned':min(len(result),limit),'sellers':result[:limit]}
