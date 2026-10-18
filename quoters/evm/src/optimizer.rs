//! Search parameters shared by the router (`router.rs`), which replaced this module's
//! direct-pool-only split search when multi-hop routes were added.

#[derive(Clone, Debug)]
pub struct Params {
    pub steps: usize,
    pub max_splits: usize,
    /// Smallest amount the local search moves, in ppm of the input.
    pub precision_ppm: u64,
    pub max_sims: usize,
    pub prune: bool,
    /// Output-token atomic units per unit of gas.
    pub gas_cost: f64,
    /// Smallest probe for the best-rate estimate (one tenth of a token).
    pub min_probe: u128,
    /// A pool's grid stops once a step's marginal rate is below this fraction of its best
    /// rate (0 = only at failure).
    pub cutoff: f64,
    /// Fill the input in full even past a 10% sqrt-price move (walks every tick; slow).
    pub full_fill: bool,
    /// Two-hop intermediates (symbols); None = the chain's default, empty = direct pools only.
    pub via: Option<Vec<String>>,
    /// Longest route considered: 1 = direct pools only, 2 = two hops, 3 = three hops.
    pub max_hops: usize,
}
