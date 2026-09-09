from travel_agent.composition import create_simulated_coordinator

def main():
    coordinator = create_simulated_coordinator()

    result = coordinator.plan_trip(
        flight_number="DL1425",
        date="2026-09-11",
    )

    print("\n=== RECOMMENDED PLAN ===")
    print("Status:", result["status"])
    print(result["recommendation"])

    print("\n=== FINALISTS ===")
    for i, plan in enumerate(result["finalists"], 1):
        print(f"{i}. {plan['label']} | score={plan['score']:.2f}")
        print(f"   {plan['summary']}")
        print("   Feasibility:", plan["feasibility"])

if __name__ == "__main__":
    main()
