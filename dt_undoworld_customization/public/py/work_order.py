
import frappe
from frappe.utils import now

@frappe.whitelist()
def run_workstation_logic(work_order_name):
    doc = frappe.get_doc("Work Order", work_order_name)

    # Get the latest workstation from existing item movements
    if doc.custom_workstation_item_movement:
        latest_entry = sorted(doc.custom_workstation_item_movement, key=lambda x: x.arrival_time)[-1]
        latest_ws_name = latest_entry.workstation
        current_ws = frappe.get_doc("Workstation", latest_ws_name)
    else:
        first_ws=frappe.db.get_value("Workstation",{"custom_is_first_workstation":1},"name")
        current_ws = frappe.get_doc("Workstation", first_ws)


    to_warehouse = current_ws.warehouse
    existing_item_keys = {
        (row.item,  row.serial_no,row.workstation) for row in doc.custom_workstation_item_movement
    }

    new_items = []
    for item in doc.required_items:
        key = (item.item_code, item.custom_serial_number,current_ws.name)
        if key not in existing_item_keys and not item.custom_is_return:
            new_items.append(item)

    if not new_items:
        return "No new items to add."

    # Create ONE stock entry for all new items to this workstation
    stock_entry = frappe.new_doc("Stock Entry")
    stock_entry.stock_entry_type = "Material Transfer"
    stock_entry.purpose = "Material Transfer"
    stock_entry.flags.ignore_permissions = True
    stock_entry.company = doc.company

    for item in new_items:
        stock_entry.append("items", {
            "item_code": item.item_code,
            "qty": item.required_qty,
            "use_serial_batch_fields":1,
            "serial_no": item.custom_serial_number,
            "s_warehouse": item.source_warehouse or "Stores - " + doc.company[:3],
            "t_warehouse": to_warehouse,
            "allow_zero_valuation_rate":1
        })

    stock_entry.insert()
    stock_entry.submit()

    # Add all to workstation movement table
    for item in new_items:
        doc.append("custom_workstation_item_movement", {
            "workstation": current_ws.name,
            "workstation_warehouse": to_warehouse,
            "item": item.item_code,
            "qty": item.required_qty,
            "serial_no": item.custom_serial_number,
            "arrival_time": now(),
            "stock_entry": stock_entry.name
        })
    doc.custom_current_workstation=current_ws.name
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    frappe.msgprint(f"{len(new_items)} item(s) moved to {current_ws.name} with Stock Entry {stock_entry.name}.")

    process_old_parts(work_order_name)

    return 1


@frappe.whitelist()
def move_to_next_workstation(work_order_name, selected_workstation):
    doc = frappe.get_doc("Work Order", work_order_name)

    if not doc.custom_workstation_item_movement:
        frappe.throw("No items found in current workstation.")

    latest_entries = sorted(doc.custom_workstation_item_movement, key=lambda x: x.arrival_time, reverse=True)
    current_ws_name = latest_entries[0].workstation    

    returned_items = []

    spare_parts_warehouse = "Spare Part Warehouse - UW"

    for req in doc.required_items:
        if req.custom_is_return and not req.custom_return_entry:
            returned_items.append(req)

    if returned_items:
        return_stock_entry = frappe.new_doc("Stock Entry")
        return_stock_entry.stock_entry_type = "Material Transfer"
        return_stock_entry.purpose = "Material Transfer"
        return_stock_entry.company = doc.company
        return_stock_entry.flags.ignore_permissions = True

        for item in returned_items:
            # Find the last known workstation warehouse for the item
            last_ws_row = next((x for x in reversed(latest_entries) if x.item == item.item_code and x.serial_no == item.custom_serial_number), None)
            if not last_ws_row:
                continue

            return_stock_entry.append("items", {
                "item_code": item.item_code,
                "qty": item.required_qty or 1,
                "serial_no": item.custom_serial_number,
                "s_warehouse": last_ws_row.workstation_warehouse,
                "t_warehouse": spare_parts_warehouse,
                "use_serial_batch_fields": 1,
                "allow_zero_valuation_rate": 1
            })

        if return_stock_entry.items:
            return_stock_entry.insert()
            return_stock_entry.submit()

            # Link return entry to required items
            for item in returned_items:
                item.custom_return_entry = return_stock_entry.name
    


    # Get the selected workstation and its warehouse
    selected_ws = frappe.get_doc("Workstation", selected_workstation)
    to_warehouse = selected_ws.warehouse

    # Prepare the stock entry for the next workstation
    stock_entry = frappe.new_doc("Stock Entry")
    stock_entry.stock_entry_type = "Material Transfer"
    stock_entry.purpose = "Material Transfer"
    stock_entry.company = doc.company
    stock_entry.flags.ignore_permissions = True

    # Initialize new movements for the selected workstation
    new_movements = []

    for row in latest_entries:

        if row.workstation != current_ws_name:
            continue

        is_returned = False
        for req in doc.required_items:
            if req.item_code == row.item and req.custom_serial_number == row.serial_no and req.custom_is_return:
                is_returned = True
                break

        if is_returned:
            continue    

        stock_entry.append("items", {
            "item_code": row.item,
            "qty": row.qty,
            "s_warehouse": row.workstation_warehouse,
            "t_warehouse": to_warehouse,
            "serial_no": row.serial_no,
            "use_serial_batch_fields": 1,
            "allow_zero_valuation_rate":1
        })
        new_movements.append({
            "workstation": selected_ws.name,
            "workstation_warehouse": to_warehouse,
            "item": row.item,
            "qty": row.qty,
            "arrival_time": now(),
            "serial_no": row.serial_no,
            "stock_entry": None
        })

    # If new movements are created, save them
    if new_movements:
        new_movements = sorted(new_movements, key=lambda x: x["item"])

        for movement in new_movements:
            doc.append("custom_workstation_item_movement", movement)

        stock_entry.insert()
        stock_entry.submit()

        # Link the stock entry to the movements
        for movement in doc.custom_workstation_item_movement:
            if movement.workstation == selected_ws.name:
                movement.stock_entry = stock_entry.name
        doc.custom_current_workstation= selected_ws.name
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        process_old_parts(work_order_name)

        return f"Moved {len(new_movements)} item(s) to {selected_ws.name} with Stock Entry {stock_entry.name}."
    else:
        return



@frappe.whitelist()
def on_submit_work(work_order_name):
    # Get the Work Order document
    doc = frappe.get_doc("Work Order", work_order_name)

    # Ensure there's data in the custom_workstation_item_movement table
    if not doc.custom_workstation_item_movement:
        return "No items in the workstation movement table to update."

    latest_entry = max(doc.custom_workstation_item_movement, key=lambda x: x.arrival_time)
    current_ws = latest_entry.workstation_warehouse
    for item in doc.required_items:
        if not item.custom_is_return:
            frappe.db.set_value("Work Order Item", item.name, "source_warehouse", current_ws, update_modified=False)
        elif item.custom_return_entry:
            frappe.db.set_value("Work Order Item", item.name, "source_warehouse", "Spare Part Warehouse - UW", update_modified=False)



    frappe.db.commit()

    return f"Updated source warehouse for {len(doc.required_items)} item(s) in the {current_ws} workstation."



@frappe.whitelist()
def process_old_parts(work_order_name):
    # Fetch the Work Order
    doc = frappe.get_doc("Work Order", work_order_name)

    old_parts_warehouse = "Old Parts Warehouse - UW"

    stock_entry = frappe.new_doc("Stock Entry")
    stock_entry.stock_entry_type = "Material Receipt"
    stock_entry.company = doc.company
    stock_entry.flags.ignore_permissions = True

    added_items = []

    for required_item in doc.required_items:
        item_code = required_item.item_code
        custom_serial_no = required_item.custom_serial_number

        # Fetch Item document and check conditions
        if required_item.custom_is_part_missing or required_item.custom_is_old_phone or (not custom_serial_no):
            continue
        # Find an active serial number in any set warehouse
        # serial_doc = frappe.db.get_list(
        #     "Serial No",
        #     filters={
        #         "name":custom_serial_no,
        #         "warehouse": required_item.source_warehouse,
        #         "status": "Active"
        #     },
        #     fields=["name"],
        # )
        # print(custom_serial_no,required_item.source_warehouse,serial_doc)
        # if not serial_doc:
        #     continue
        # Add to stock entry
        stock_entry.append("items", {
            "item_code": item_code,
            "qty": required_item.required_qty,
            "t_warehouse": old_parts_warehouse,
            "allow_zero_valuation_rate":1
            })

        existing = any(
            d.serial_no == custom_serial_no and d.item == item_code and d.target_warehouse == old_parts_warehouse
            for d in doc.custom_old_part_movement_table
        )
        if not existing:
            new_entry = {
                "item": item_code,
                "qty": required_item.required_qty,
                "target_warehouse": old_parts_warehouse,
                "stock_entry": None,
                "serial_no":custom_serial_no
            }
            doc.append("custom_old_part_movement_table", new_entry)
            added_items.append(item_code)

    if added_items:
        stock_entry.insert()

        for movement in doc.custom_old_part_movement_table:
            movement.stock_entry = stock_entry.name

        stock_entry.submit()
        doc.save(ignore_permissions=True)
        frappe.db.commit()


