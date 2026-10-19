//! A scripted JSON-RPC node for mirror tests: per-block account state and logs, served over
//! HTTP on 127.0.0.1, with switches that imitate a load-balanced public endpoint (PublicNode)
//! whose nodes lag, drop logs, or have pruned old state.

use revm::primitives::{hex, keccak256, Address, Bytes, HashMap, HashSet, B256, U256};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

#[derive(Clone, Default)]
pub struct Acct {
    pub balance: U256,
    pub code: Bytes,
    pub storage: HashMap<U256, U256>,
}

#[derive(Clone, Default)]
pub struct Block {
    pub accounts: HashMap<Address, Acct>,
    /// Contracts that emitted a log in this block.
    pub logs: Vec<Address>,
}

#[derive(Default)]
pub struct Chain {
    pub blocks: BTreeMap<u64, Block>,
    /// State more than this many blocks below the head is refused like PublicNode's archive tier.
    pub window: u64,
    /// The next this-many HTTP requests that query logs reach a node one block behind. It
    /// returns the logs it has without an error (and no header for the head block).
    pub lagging_log_requests: u32,
    /// The next this-many HTTP requests that read state reach a node one block behind.
    pub lagging_state_requests: u32,
    /// Logs of these contracts are never returned.
    pub dropped_logs: HashSet<Address>,
    /// State reads that used the `latest` tag (unpinned).
    pub latest_reads: u32,
}

const STATE_METHODS: &[&str] = &["eth_getBalance", "eth_getTransactionCount", "eth_getCode", "eth_getStorageAt", "eth_getProof"];

fn hexn(n: u64) -> Value {
    json!(format!("0x{n:x}"))
}

fn hexu(v: U256) -> Value {
    json!(format!("{v:#x}"))
}

fn num(v: &Value) -> u64 {
    u64::from_str_radix(v.as_str().unwrap().trim_start_matches("0x"), 16).unwrap()
}

fn addr(v: &Value) -> Address {
    v.as_str().unwrap().parse().unwrap()
}

fn key(v: &Value) -> U256 {
    U256::from_str_radix(v.as_str().unwrap().trim_start_matches("0x"), 16).unwrap()
}

type Answer = Result<Value, (i64, String)>;

impl Chain {
    pub fn head(&self) -> u64 {
        *self.blocks.keys().next_back().unwrap()
    }

    pub fn hash(n: u64) -> B256 {
        keccak256(n.to_be_bytes())
    }

    /// Appends a block: a copy of the head with `f` applied, in which `logs` emitted.
    pub fn mine(&mut self, logs: &[Address], f: impl FnOnce(&mut HashMap<Address, Acct>)) -> u64 {
        let n = self.head() + 1;
        let mut b = self.blocks[&(n - 1)].clone();
        b.logs = logs.to_vec();
        f(&mut b.accounts);
        self.blocks.insert(n, b);
        n
    }

    fn header(&self, n: u64) -> Value {
        json!({
            "number": hexn(n), "hash": Self::hash(n), "timestamp": hexn(1_700_000_000 + 12 * n),
            "baseFeePerGas": "0x1", "gasLimit": "0x1c9c380", "mixHash": B256::ZERO, "miner": Address::ZERO,
        })
    }

    /// The block a state read at `tag` resolves to on a node whose head is `head`.
    fn state_block(&mut self, tag: &Value, head: u64) -> Result<&Block, (i64, String)> {
        let n = if tag.as_str() == Some("latest") {
            self.latest_reads += 1;
            head
        } else {
            num(tag)
        };
        if n > head {
            return Err((-32000, format!("block not found: {n}")));
        }
        if n + self.window < self.head() {
            return Err((-32602, "Archive requests require a personal token. Get one at: https://www.allnodes.com/publicnode".into()));
        }
        Ok(&self.blocks[&n])
    }

    fn answer(&mut self, req: &Value, head: u64) -> Value {
        let p = &req["params"];
        let empty = Acct::default();
        let r: Answer = match req["method"].as_str().unwrap() {
            "eth_blockNumber" => Ok(hexn(head)),
            "eth_gasPrice" => Ok(json!("0x1")),
            "eth_getBlockByNumber" => {
                let n = if p[0].as_str() == Some("latest") { head } else { num(&p[0]) };
                Ok(if n <= head { self.header(n) } else { Value::Null })
            }
            "eth_getBalance" => self.state_block(&p[1], head).map(|b| hexu(b.accounts.get(&addr(&p[0])).unwrap_or(&empty).balance)),
            "eth_getTransactionCount" => self.state_block(&p[1], head).map(|_| json!("0x0")),
            "eth_getCode" => self
                .state_block(&p[1], head)
                .map(|b| json!(format!("0x{}", hex::encode(&b.accounts.get(&addr(&p[0])).unwrap_or(&empty).code)))),
            "eth_getStorageAt" => self.state_block(&p[2], head).map(|b| {
                let a = b.accounts.get(&addr(&p[0])).unwrap_or(&empty);
                hexu(a.storage.get(&key(&p[1])).copied().unwrap_or_default())
            }),
            "eth_getProof" => self.state_block(&p[2], head).map(|b| {
                let a = b.accounts.get(&addr(&p[0])).unwrap_or(&empty);
                let proofs: Vec<Value> = p[1]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|k| json!({"key": k, "value": hexu(a.storage.get(&key(k)).copied().unwrap_or_default()), "proof": []}))
                    .collect();
                json!({"balance": hexu(a.balance), "storageProof": proofs})
            }),
            "eth_getLogs" => {
                let f = &p[0];
                let want: HashSet<Address> = f["address"].as_array().unwrap().iter().map(addr).collect();
                let (from, to) = (num(&f["fromBlock"]), num(&f["toBlock"]));
                let mut logs = Vec::new();
                for n in from..=to.min(head) {
                    for a in &self.blocks[&n].logs {
                        if want.contains(a) && !self.dropped_logs.contains(a) {
                            logs.push(json!({"address": a, "blockNumber": hexn(n)}));
                        }
                    }
                }
                Ok(Value::Array(logs))
            }
            m => Err((-32601, format!("method {m} not found"))),
        };
        match r {
            Ok(v) => json!({"jsonrpc": "2.0", "id": req["id"], "result": v}),
            Err((code, message)) => json!({"jsonrpc": "2.0", "id": req["id"], "error": {"code": code, "message": message}}),
        }
    }

    fn handle(&mut self, body: &Value) -> Value {
        let items: Vec<&Value> = match body {
            Value::Array(a) => a.iter().collect(),
            one => vec![one],
        };
        let has = |ms: &[&str]| items.iter().any(|i| ms.contains(&i["method"].as_str().unwrap_or("")));
        // One HTTP request reaches one node.
        let mut lag = 0;
        if has(&["eth_getLogs"]) && self.lagging_log_requests > 0 {
            self.lagging_log_requests -= 1;
            lag = 1;
        } else if has(STATE_METHODS) && self.lagging_state_requests > 0 {
            self.lagging_state_requests -= 1;
            lag = 1;
        }
        let head = self.head() - lag;
        let out: Vec<Value> = items.iter().map(|i| self.answer(i, head)).collect();
        if body.is_array() { Value::Array(out) } else { out.into_iter().next().unwrap() }
    }
}

pub struct Node {
    pub url: String,
    pub chain: Arc<Mutex<Chain>>,
}

impl Node {
    pub fn start(chain: Chain) -> Node {
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let url = format!("http://{}", server.server_addr().to_ip().unwrap());
        let chain = Arc::new(Mutex::new(chain));
        let c = chain.clone();
        std::thread::spawn(move || {
            for mut req in server.incoming_requests() {
                let mut body = String::new();
                req.as_reader().read_to_string(&mut body).unwrap();
                let v: Value = serde_json::from_str(&body).unwrap();
                let resp = c.lock().unwrap().handle(&v);
                let _ = req.respond(tiny_http::Response::from_string(resp.to_string()));
            }
        });
        Node { url, chain }
    }
}
