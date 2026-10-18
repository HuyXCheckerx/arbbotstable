//! One transaction in revm against a mirror view, never committed.

use crate::mirror::{BlockCtx, View};
use revm::context::result::ExecutionResult;
use revm::context::{BlockEnv, TxEnv};
use revm::database_interface::WrapDatabaseRef;
use revm::primitives::hardfork::SpecId;
use revm::primitives::{Address, Bytes, TxKind, U256};
use revm::state::EvmState;
use revm::{Context, ExecuteEvm, MainBuilder, MainContext};

pub struct ChainEnv {
    pub chain_id: u64,
    pub spec: SpecId,
    pub block_time_ms: u64,
}

pub struct Exec {
    pub success: bool,
    pub output: Bytes,
    pub gas_used: u64,
    pub state: EvmState,
    pub halt: Option<String>,
}

pub const GAS_LIMIT: u64 = 12_000_000;

/// Executes `data` as a call from `caller` to `to` in the block after the mirrored one.
pub fn exec(view: View<'_>, env: &ChainEnv, head: &BlockCtx, caller: Address, to: Address, data: Bytes) -> Exec {
    let block = BlockEnv {
        number: U256::from(head.number + 1),
        beneficiary: head.beneficiary,
        timestamp: U256::from(head.timestamp + env.block_time_ms.div_ceil(1000).max(1)),
        gas_limit: head.gas_limit.max(GAS_LIMIT * 2),
        basefee: head.basefee,
        difficulty: U256::ZERO,
        prevrandao: Some(head.prevrandao),
        ..Default::default()
    };
    let ctx = Context::mainnet()
        .with_db(WrapDatabaseRef(view))
        .with_block(block)
        .modify_cfg_chained(|c| {
            c.spec = env.spec;
            c.chain_id = env.chain_id;
            c.disable_nonce_check = true;
            c.disable_balance_check = true;
            c.disable_base_fee = true;
            c.disable_eip3607 = true;
            c.disable_block_gas_limit = true;
            c.disable_fee_charge = true;
            c.tx_gas_limit_cap = Some(u64::MAX);
            c.limit_contract_code_size = Some(usize::MAX);
        });
    let mut evm = ctx.build_mainnet();
    let tx = TxEnv {
        caller,
        kind: TxKind::Call(to),
        data,
        gas_limit: GAS_LIMIT,
        gas_price: head.gas_price.max(head.basefee as u128),
        gas_priority_fee: None,
        chain_id: Some(env.chain_id),
        nonce: 0,
        value: U256::ZERO,
        tx_type: 0,
        ..Default::default()
    };
    match evm.transact(tx) {
        Ok(r) => {
            let gas_used = r.result.gas_used();
            match r.result {
                ExecutionResult::Success { output, .. } => Exec {
                    success: true,
                    output: output.into_data(),
                    gas_used,
                    state: r.state,
                    halt: None,
                },
                ExecutionResult::Revert { output, .. } => {
                    Exec { success: false, output, gas_used, state: r.state, halt: None }
                }
                ExecutionResult::Halt { reason, .. } => Exec {
                    success: false,
                    output: Bytes::new(),
                    gas_used,
                    state: r.state,
                    halt: Some(format!("{reason:?}")),
                },
            }
        }
        Err(e) => Exec {
            success: false,
            output: Bytes::new(),
            gas_used: 0,
            state: Default::default(),
            halt: Some(format!("{e:?}")),
        },
    }
}

/// Decodes a Solidity `Error(string)` revert, else hex.
pub fn revert_reason(out: &Bytes) -> String {
    if out.len() >= 68 && out[..4] == [0x08, 0xc3, 0x79, 0xa0] {
        let len = U256::from_be_slice(&out[36..68]).to::<usize>().min(out.len() - 68);
        return String::from_utf8_lossy(&out[68..68 + len]).into_owned();
    }
    if out.is_empty() {
        return "empty revert".into();
    }
    format!("0x{}", revm::primitives::hex::encode(&out[..out.len().min(68)]))
}
