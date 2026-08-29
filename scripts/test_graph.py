from documind.agents.graph import build_graph

app = build_graph()

test_questions = [
    "What cloud technologies does this person have experience with?",
    "What about that?",
    "Write me a haiku about the ocean.",
]

for question in test_questions:
    result = app.invoke({"question": question, "retry_count": 0, "retrieved_chunks": []})
    print(f"Q: {question}")
    print(f"A: {result['final_answer']}\n")