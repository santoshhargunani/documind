from documind.agents.retriever import retrieve_chunks
from documind.agents.synthesis import synthesize_answer

state = {"question": "What cloud technologies does this person have experience with?"}
state.update(retrieve_chunks(state))
state.update(synthesize_answer(state))

print(f"Question: {state['question']}\n")
print(f"Answer:\n{state['draft_answer']}")