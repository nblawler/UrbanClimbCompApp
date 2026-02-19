# seed_competitors.py
from app import db, Competitor, run

def main(num_competitors=500):
    with run.app_context():
        existing = Competitor.query.count()
        print(f"Existing competitors: {existing}")

        # Create new competitors starting after the highest ID
        for i in range(num_competitors):
            c = Competitor(
                name=f"Test Competitor {existing + i + 1}",
                gender="Inclusive"
            )
            db.session.add(c)

        db.session.commit()
        total = Competitor.query.count()
        print(f"Now have {total} competitors in the DB.")

if __name__ == "__main__":
    main()
