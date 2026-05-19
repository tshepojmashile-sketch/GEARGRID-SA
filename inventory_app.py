import sqlite3
from datetime import datetime
import time

# DATABASE
conn = sqlite3.connect("rental.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    status TEXT,
    rented_to TEXT,
    due_date TEXT,
    alerted INTEGER DEFAULT 0
)
""")

conn.commit()

# FUNCTIONS

def add_equipment():
    name = input("Enter equipment name: ")
    cursor.execute(
        "INSERT INTO equipment (name, status) VALUES (?, ?)",
        (name, "available")
    )
    conn.commit()
    print("Equipment added.\n")


def rent_equipment():
    view_all()

    equipment_id = int(input("Enter equipment ID to rent: "))
    client = input("Enter client name: ")
    due_date = input("Enter due date (YYYY-MM-DD): ")

    cursor.execute("""
    UPDATE equipment
    SET status = 'rented',
        rented_to = ?,
        due_date = ?,
        alerted = 0
    WHERE id = ?
    """, (client, due_date, equipment_id))

    conn.commit()
    print("Equipment rented.\n")


def return_equipment():
    view_all()

    equipment_id = int(input("Enter equipment ID to return: "))

    cursor.execute("""
    UPDATE equipment
    SET status = 'available',
        rented_to = NULL,
        due_date = NULL,
        alerted = 0
    WHERE id = ?
    """, (equipment_id,))

    conn.commit()
    print("Equipment returned.\n")


def view_all():
    cursor.execute("SELECT * FROM equipment")
    rows = cursor.fetchall()

    print("\nINVENTORY:\n")
    for r in rows:
        print(r)
    print()


def check_overdue():
    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute("""
    SELECT id, name, rented_to, due_date, alerted
    FROM equipment
    WHERE status = 'rented'
    """)

    rows = cursor.fetchall()

    for row in rows:
        id, name, client, due_date, alerted = row

        if due_date and due_date < today and alerted == 0:
            print(f"\n🚨 ALERT: {name} rented by {client} is OVERDUE!")

            cursor.execute(
                "UPDATE equipment SET alerted = 1 WHERE id = ?",
                (id,)
            )
            conn.commit()


def start_monitoring():
    print("\nMonitoring overdue items...\n")

    while True:
        check_overdue()
        time.sleep(5)


# MAIN MENU
while True:
    print("====== RENTAL SYSTEM ======")
    print("1. Add Equipment")
    print("2. Rent Equipment")
    print("3. Return Equipment")
    print("4. View Inventory")
    print("5. Start Monitoring Alerts")
    print("6. Exit")

    choice = input("Choose an option: ")

    if choice == "1":
        add_equipment()
    elif choice == "2":
        rent_equipment()
    elif choice == "3":
        return_equipment()
    elif choice == "4":
        view_all()
    elif choice == "5":
        start_monitoring()
    elif choice == "6":
        break
    else:
        print("Invalid choice\n")

conn.close()
