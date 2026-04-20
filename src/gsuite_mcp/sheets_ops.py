"""Google Sheets v4 operations — append rows."""

import asyncio
from typing import Any


async def append_rows(
    sheets_service, file_id: str, content: str
) -> dict[str, Any]:
    """Append rows to the first sheet of a spreadsheet.

    Content is split on newlines into rows, then on commas into columns.
    Uses USER_ENTERED so formulas are evaluated.
    """
    meta = await asyncio.to_thread(
        lambda: sheets_service.spreadsheets()
        .get(spreadsheetId=file_id, fields="sheets(properties(title))")
        .execute()
    )
    first_sheet_title = meta["sheets"][0]["properties"]["title"]

    rows = [
        [cell.strip() for cell in line.split(",")]
        for line in content.splitlines()
        if line.strip()
    ]

    await asyncio.to_thread(
        lambda: sheets_service.spreadsheets()
        .values()
        .append(
            spreadsheetId=file_id,
            range=first_sheet_title,
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        )
        .execute()
    )
    return {"bytes_appended": len(content.encode("utf-8")), "rows_added": len(rows)}
