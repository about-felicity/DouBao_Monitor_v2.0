# Yingdao Doubao reference workflow

This workflow assumes every run creates a new mobile Doubao chat and sends the same prompt:

```text
推荐一款染发剂
```

## Chrome extension selectors

Use these selectors in Yingdao Web automation:

```text
Grab button: #__doubao_ref_button
Result textarea: #__doubao_ref_panel_textarea
Status input: #__doubao_ref_status
Count input: #__doubao_ref_count
Complete input: #__doubao_ref_complete
Chat title input: #__doubao_ref_chat_title
```

Read `value` from inputs and textarea.

## Recommended flow

1. Mobile: create a new chat.
2. Mobile: send `推荐一款染发剂`.
3. Wait 10 seconds.
4. Web: click the first/latest item in the left history list.
5. Web: get current page text.
6. If page text does not contain `推荐一款染发剂`, wait 2 seconds and retry step 4, up to 10 times.
7. Web: click `#__doubao_ref_latest_grab`.
8. Wait 1 second.
9. Web: read `#__doubao_ref_status` value.
10. If status is `running`, wait 1 second and read again, up to 10 times.
11. Web: read `#__doubao_ref_complete` value.
12. If complete is `true`, read `#__doubao_ref_panel_textarea` value and parse JSON.
13. If complete is not `true`, click `#__doubao_ref_latest_grab` again and retry up to 3 times.

## Simplified loop

Use this when every run starts a new mobile chat and sends the same prompt.

```text
Loop N times:
  Mobile: create new chat
  Mobile: input 推荐一款染发剂
  Mobile: send
  Wait 10 seconds
  Web: click #__doubao_ref_latest_grab
  Web: wait until #__doubao_ref_status value is done
  Web: read #__doubao_ref_complete value
  If complete is true:
    Web: read #__doubao_ref_panel_textarea value into ref_json
    Python: call save_doubao_refs.py with ref_json
  Else:
    retry latest+grab up to 3 times
```

## Save JSON from Yingdao Python

After reading the textarea `value` into `ref_json`, insert Python:

```python
import subprocess

subprocess.check_output(
    [
        "python",
        r"C:\Users\AMD\Desktop\monitor\save_doubao_refs.py",
        ref_json,
    ],
    cwd=r"C:\Users\AMD\Desktop\monitor",
    encoding="utf-8",
    errors="ignore",
)
```

## Fully automatic Python control from Yingdao

After the mobile side sends the message and waits 10 seconds, insert Python:

```python
import subprocess

result = subprocess.check_output(
    [
        "python",
        r"C:\Users\AMD\Desktop\monitor\run_doubao_latest_grab.py",
    ],
    cwd=r"C:\Users\AMD\Desktop\monitor",
    encoding="utf-8",
    errors="ignore",
)

print(result)
```

This Python script connects to Chrome on port `9222`, clicks the extension button
`#__doubao_ref_latest_grab`, waits until the plugin finishes, reads the JSON, and
appends the rows to:

```text
C:\Users\AMD\Desktop\monitor\doubao_refs_result.csv
C:\Users\AMD\Desktop\monitor\doubao_refs_result.xlsx
```

Important: Doubao Web must be opened in the Chrome started by:

```text
C:\Users\AMD\Desktop\monitor\open_chrome_debug.bat
```

## Result JSON

The textarea contains JSON like:

```json
{
  "ok": true,
  "status": "done",
  "count": 10,
  "expectedCount": 10,
  "complete": true,
  "url": "https://www.doubao.com/chat/...",
  "chatTitle": "染发剂推荐",
  "items": [
    {
      "index": 1,
      "title": "公认零差评的染发膏推荐！...",
      "href": "https://www.iesdouyin.com/share/video/...",
      "source": "search_query_result a[href]"
    }
  ]
}
```

## Key rule

Because every prompt is the same, do not use the prompt alone to identify the newest chat.

Use this order:

```text
Click latest Web history item -> confirm page contains prompt -> grab links.
```
