//! Public quote APIs reachable without keys from this machine (2026-10-17): ParaSwap (Velora),
//! CoW Swap and LI.FI. KyberSwap is geo-blocked here, every Odos endpoint returned
//! Cloudflare error 1033, OpenOcean serves a bot challenge, and 1inch and 0x need keys.

use crate::chains::{Chain, Token};
use serde::Serialize;
use serde_json::{json, Value};
use std::time::Instant;

#[derive(Serialize, Clone, Debug, Default)]
pub struct AggQuote {
    pub name: String,
    /// Output before network fees, atomic (LI.FI: its 0.25% fixed fee added back;
    /// CoW: scaled from the fee-reduced sell amount to the full one).
    pub gross: Option<u128>,
    /// Output after the aggregator's own gas estimate, in tokens.
    pub net: Option<f64>,
    pub raw_out: Option<String>,
    pub gas: Option<u64>,
    pub gas_usd: Option<f64>,
    pub ms: u128,
    pub route: String,
    pub err: Option<String>,
}

const FROM: &str = "0x000000000000000000000000000000000000dEaD";

fn get(agent: &ureq::Agent, url: &str, body: Option<&Value>) -> Result<Value, String> {
    let r = match body {
        Some(b) => agent.post(url).header("content-type", "application/json").send_json(b),
        None => agent.get(url).header("accept", "application/json").call(),
    };
    let mut r = r.map_err(|e| e.to_string())?;
    let status = r.status().as_u16();
    let text = r.body_mut().with_config().limit(64 << 20).read_to_string().map_err(|e| e.to_string())?;
    let v: Value = serde_json::from_str(&text).map_err(|_| format!("HTTP {status}: {}", &text[..text.len().min(160)]))?;
    if status != 200 {
        return Err(format!("HTTP {status}: {}", &v.to_string()[..v.to_string().len().min(200)]));
    }
    Ok(v)
}

fn num(v: &Value) -> Option<u128> {
    v.as_str().and_then(|s| s.parse().ok()).or_else(|| v.as_u64().map(|x| x as u128))
}

fn tokens(atomic: u128, t: &Token) -> f64 {
    atomic as f64 / 10f64.powi(t.decimals as i32)
}

pub fn paraswap(agent: &ureq::Agent, c: &Chain, tin: Token, tout: Token, amount: u128, exclude_rfq: bool) -> AggQuote {
    let name = if exclude_rfq { "ParaSwap (no RFQ)" } else { "ParaSwap" };
    let url = format!(
        "https://api.paraswap.io/prices?srcToken={:?}&destToken={:?}&amount={amount}&srcDecimals={}&destDecimals={}&side=SELL&network={}&version=6.2{}",
        tin.address,
        tout.address,
        tin.decimals,
        tout.decimals,
        c.paraswap,
        if exclude_rfq { "&excludeRFQ=true" } else { "" }
    );
    // The keyless tier answers 429 after a handful of requests; back off and retry.
    let mut r = Err(String::new());
    let mut ms = 0;
    for attempt in 0..4 {
        let t = Instant::now();
        r = get(agent, &url, None);
        ms = t.elapsed().as_millis();
        match &r {
            Err(e) if e.contains("429") && attempt < 3 => std::thread::sleep(std::time::Duration::from_secs(5 << attempt)),
            _ => break,
        }
    }
    match r {
        Ok(v) => {
            let p = &v["priceRoute"];
            let out = num(&p["destAmount"]);
            let gas_usd = p["gasCostUSD"].as_str().and_then(|s| s.parse::<f64>().ok());
            let mut ex: Vec<String> = Vec::new();
            for r in p["bestRoute"].as_array().into_iter().flatten() {
                for s in r["swaps"].as_array().into_iter().flatten() {
                    for e in s["swapExchanges"].as_array().into_iter().flatten() {
                        ex.push(format!("{} {}%", e["exchange"].as_str().unwrap_or("?"), e["percent"]));
                    }
                }
            }
            AggQuote {
                name: name.into(),
                gross: out,
                net: out.map(|o| tokens(o, &tout) - gas_usd.unwrap_or(0.0)),
                raw_out: out.map(|o| o.to_string()),
                gas: num(&p["gasCost"]).map(|g| g as u64),
                gas_usd,
                ms,
                route: ex.join(", "),
                err: if out.is_none() { Some(v.to_string()[..200.min(v.to_string().len())].into()) } else { None },
            }
        }
        Err(e) => AggQuote { name: name.into(), ms, err: Some(e), ..Default::default() },
    }
}

pub fn cow(agent: &ureq::Agent, c: &Chain, tin: Token, tout: Token, amount: u128) -> AggQuote {
    let Some(net) = c.cow else { return AggQuote { name: "CoW".into(), err: Some("chain not supported".into()), ..Default::default() } };
    let body = json!({
        "sellToken": tin.address, "buyToken": tout.address, "from": FROM, "receiver": FROM, "kind": "sell",
        "sellAmountBeforeFee": amount.to_string(), "signingScheme": "eip712", "onchainOrder": false, "priceQuality": "optimal"
    });
    let t = Instant::now();
    let r = get(agent, &format!("https://api.cow.fi/{net}/api/v1/quote"), Some(&body));
    let ms = t.elapsed().as_millis();
    match r {
        Ok(v) => {
            let q = &v["quote"];
            let buy = num(&q["buyAmount"]);
            let sell = num(&q["sellAmount"]);
            let gross = match (buy, sell) {
                (Some(b), Some(s)) if s > 0 => Some(((b as f64) * amount as f64 / s as f64).round() as u128),
                _ => None,
            };
            AggQuote {
                name: "CoW".into(),
                gross,
                net: buy.map(|b| tokens(b, &tout)),
                raw_out: buy.map(|b| b.to_string()),
                gas: num(&q["gasAmount"]).map(|g| g as u64),
                gas_usd: None,
                ms,
                route: format!("fee {} {}", q["feeAmount"].as_str().unwrap_or("?"), tin.symbol),
                err: if buy.is_none() { Some(v.to_string()[..200.min(v.to_string().len())].into()) } else { None },
            }
        }
        Err(e) => AggQuote { name: "CoW".into(), ms, err: Some(e), ..Default::default() },
    }
}

pub fn lifi(agent: &ureq::Agent, c: &Chain, tin: Token, tout: Token, amount: u128) -> AggQuote {
    let url = format!(
        "https://li.quest/v1/quote?fromChain={id}&toChain={id}&fromToken={:?}&toToken={:?}&fromAmount={amount}&fromAddress={FROM}&slippage=0.005",
        tin.address,
        tout.address,
        id = c.chain_id
    );
    let t = Instant::now();
    let r = get(agent, &url, None);
    let ms = t.elapsed().as_millis();
    match r {
        Ok(v) => {
            let e = &v["estimate"];
            let to = num(&e["toAmount"]);
            // The LI.FI fixed fee (0.25%) is taken from the input; scale back to the full input.
            let fee: u128 = e["feeCosts"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|f| f["included"].as_bool().unwrap_or(true))
                .filter_map(|f| num(&f["amount"]))
                .sum();
            let gross = to.map(|o| if fee < amount { ((o as f64) * amount as f64 / (amount - fee) as f64).round() as u128 } else { o });
            let gas_usd: f64 = e["gasCosts"].as_array().into_iter().flatten().filter_map(|g| g["amountUSD"].as_str()?.parse::<f64>().ok()).sum();
            let gas: u64 = e["gasCosts"].as_array().into_iter().flatten().filter_map(|g| num(&g["estimate"])).sum::<u128>() as u64;
            AggQuote {
                name: "LI.FI".into(),
                gross,
                net: gross.map(|g| tokens(g, &tout) - gas_usd),
                raw_out: to.map(|o| o.to_string()),
                gas: Some(gas),
                gas_usd: Some(gas_usd),
                ms,
                route: v["tool"].as_str().unwrap_or("?").to_string(),
                err: if to.is_none() { Some(v.to_string()[..200.min(v.to_string().len())].into()) } else { None },
            }
        }
        Err(e) => AggQuote { name: "LI.FI".into(), ms, err: Some(e), ..Default::default() },
    }
}
