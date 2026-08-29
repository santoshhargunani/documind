from documind.agents.critic import critique_answer
from documind.agents.retriever import retrieve_chunks
from documind.agents.router import route_question
from documind.agents.synthesis import synthesize_answer

state = {"question": "What cloud technologies does this person have experience with?", "retry_count": 0}
state.update(route_question(state))
print(f"Route: {state['route']}\n")

state.update(retrieve_chunks(state))
state.update(synthesize_answer(state))
state.update(critique_answer(state))

print(f"Grounded: {state['is_grounded']}")
print(f"Feedback: {state['grounding_feedback']}\n")
print(f"Final answer:\n{state['final_answer']}")