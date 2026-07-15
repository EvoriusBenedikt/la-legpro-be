import re

FILE_PATH = r"c:\Users\ben\Documents\Programming\Lintasarta\self-dev\la-legpro\la-legpro-be\api\main.py"

with open(FILE_PATH, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add get_reranker() global logic
reranker_logic = """_cross_encoder = None

def get_reranker():
    global _cross_encoder
    if _cross_encoder is None:
        import os
        from sentence_transformers import CrossEncoder
        model_name = os.environ.get("RERANKER_MODEL_NAME", "cross-encoder/ms-marco-MiniLM-L-6-v2")
        print(f"Initializing CrossEncoder reranker: {model_name}")
        _cross_encoder = CrossEncoder(model_name, max_length=512)
    return _cross_encoder
"""

# Insert before get_glm_session
if "def get_reranker" not in content:
    content = content.replace("def get_glm_session", reranker_logic + "\ndef get_glm_session")

# 2. Update retrieve_contexts logic
rerank_replace = """    # Sort by descending RRF score, take top 15 for Reranking
    ranked_chunks = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:15]
    
    # Extract candidate dictionaries
    candidates = []
    for cid, score in ranked_chunks:
        if cid in chunk_data:
            candidates.append(chunk_data[cid])
            
    # --- 4. Cross-Encoder Reranking ---
    if candidates:
        try:
            reranker = get_reranker()
            # Prepare pairs: (query, text)
            # Use the base text or window_context for scoring
            pairs = [[query, cand["text"]] for cand in candidates]
            
            # Predict scores
            scores = reranker.predict(pairs)
            
            # Attach scores to candidates
            for idx, score in enumerate(scores):
                candidates[idx]["rerank_score"] = float(score)
                
            # Sort candidates by rerank_score descending
            candidates = sorted(candidates, key=lambda x: x.get("rerank_score", 0), reverse=True)
        except Exception as e:
            print(f"Reranking failed, falling back to RRF sort: {e}")
            
    # Build final context list
    for cand in candidates[:n_results]:
        contexts.append(cand)
        
    return contexts"""

# Find the end of retrieve_contexts
old_end_pattern = r"    # Sort by descending RRF score.*return contexts"
content = re.sub(old_end_pattern, rerank_replace, content, flags=re.DOTALL)

with open(FILE_PATH, "w", encoding="utf-8") as f:
    f.write(content)

print("Patch applied!")
