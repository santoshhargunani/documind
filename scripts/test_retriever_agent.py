from documind.agents.retriever import retrieve_chunks

state = {"question": "What cloud technologies does this person have experience with?"}
result = retrieve_chunks(state)

for chunk in result["retrieved_chunks"]:
    print(f"[{chunk.similarity:.4f}] {chunk.chunk_text[:150]}")