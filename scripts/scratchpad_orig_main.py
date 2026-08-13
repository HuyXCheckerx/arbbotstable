Created At: 2026-07-11T04:04:16Z
Completed At: 2026-07-11T04:04:16Z
File Path: `file:///d:/Stuff/Stable.com%20Contracts/swapstable.py`
Total Lines: 535
Total Bytes: 22047
Showing lines 370 to 448
The following code has been modified to include a line number before every line, in the format: <line_number>: <original_line>. Please note that any changes targeting the original code should remove the line number, colon, and leading space.
370:     tx = Transaction.new_unsigned(msg)
371:     tx.sign([keypair], recent_blockhash)
372:     
373:     result = client.send_transaction(tx)
374:     if result.value:
375:         print(f"[+] Stable Swap Sent: {result.value}")
376:         client.confirm_transaction(result.value, commitment=Confirmed)
377:         return True
378:     return False
379: 
380: def main():
381:     client = Client(RPC_URL, commitment=Confirmed)
382:     session = requests.Session()
383:     headers = {"Content-Type": "application/json"}
384:     if JUP_API_KEY:
385:         headers["x-api-key"] = JUP_API_KEY
386:     session.headers.update(headers)
387: 
388:     try:
389:         keypair = Keypair.from_base58_string(PRIVATE_KEY)
390:     except Exception:
391:         keypair = Keypair.from_bytes(bytes(json.loads(PRIVATE_KEY)))
392: 
393:     wallet = keypair.pubkey()
394:     print(f"[*] Wallet: {wallet}")
395: 
396:     usdc_ata = get_ata(wallet, USDC_MINT_PK, TOKEN_PROGRAM)
397:     usdg_ata = get_ata(wallet, USDG_MINT_PK, TOKEN_2022_PROGRAM)
398:     
399:     pool_usdc_pda, _ = find_pda([POOL_SEED, bytes(USDC_MINT_PK)])
400:     pool_usdc_ata = get_ata(pool_usdc_pda, USDC_MINT_PK, TOKEN_PROGRAM)
401:     
402:     pool_usdg_pda, _ = find_pda([POOL_SEED, bytes(USDG_MINT_PK)])
403:     pool_usdg_ata = get_ata(pool_usdg_pda, USDG_MINT_PK, TOKEN_2022_PROGRAM)
404: 
405:     accounts_to_sub = {
406:         "user_usdc": str(usdc_ata),
407:         "user_usdg": str(usdg_ata),
408:         "pool_usdc": str(pool_usdc_ata),
409:         "pool_usdg": str(pool_usdg
<truncated 150 bytes>
   monitor.balances["user_usdc"] = get_token_balance(client, usdc_ata)
415:     monitor.balances["user_usdg"] = get_token_balance(client, usdg_ata)
416:     monitor.balances["pool_usdc"] = get_token_balance(client, pool_usdc_ata)
417:     monitor.balances["pool_usdg"] = get_token_balance(client, pool_usdg_ata)
418:     
419:     print("[*] Starting WebSocket Balance Monitor...")
420:     monitor.start()
421:     
422:     prev_port = print_portfolio(session, client, wallet, usdc_ata, usdg_ata, "[*] BEFORE ARB")
423:     print("[*] Starting Arb Bot loop...")
424:     while True:
425:         try:
426:             usdc_bal = monitor.balances["user_usdc"] // 10**DECIMALS
427:             usdg_bal = monitor.balances["user_usdg"] // 10**DECIMALS
428:             pool_usdc = monitor.balances["pool_usdc"] // 10**DECIMALS
429:             pool_usdg = monitor.balances["pool_usdg"] // 10**DECIMALS
430: 
431:             # Preconditions: Require at least 19998 to proceed
432:             can_usdc_to_usdg = (usdc_bal >= 19998) and (pool_usdg >= 19998)
433:             can_usdg_to_usdc = (usdg_bal >= 19998) and (pool_usdc >= 19998)
434: 
435:             if not (can_usdc_to_usdg or can_usdg_to_usdc):
436:                 print(f"[*] No route with >= 19998 liquidity for disposal. "
437:                       f"Balances: User(USDC: {usdc_bal}, USDG: {usdg_bal}), "
438:                       f"Pools(USDC: {pool_usdc}, USDG: {pool_usdg}). "
439:                       f"Waiting for updates...")
440:                 monitor.update_event.wait(timeout=60)
441:                 monitor.update_event.clear()
442:                 continue
443: 
444:             probe_amount = 10000
445:             probe_amount_raw = probe_amount * 10**DECIMALS
446: 
447:             direction = None
448:             if can_usdc_to_usdg:
The above content does NOT show the entire file contents. If you need to view any lines of the file which were not shown to complete your task, call this tool again to view those lines.
