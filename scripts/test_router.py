from documind.agents.router import route_question

test_cases = [
    "What is our company's refund policy for enterprise customers?",
    "What about that?",
    "Write me a haiku about the ocean.",
]

for question in test_cases:
    result = route_question({"question": question})
    print(f"Q: {question}")
    print(f"Route: {result['route']}\n")