//! Refuses quotes that jump away from the pair's own recent quotes.
//!
//! A mirror whose state mixes blocks can price far from the market (on 2026-10-19 a DODO pool
//! whose token balances were a swap ahead of its reserves added ~8,500 tokens to quotes of
//! 40k-60k in both directions). The guard compares a quote's rate with the median rate of the
//! pair's recent quotes of a similar size. A quote that deviates by more than `max_jump_bps` is
//! refused and the server re-reads all mirrored state. If the deviation is still there after
//! that full re-read (a real market move), it is accepted and becomes the new reference.

use revm::primitives::Address;

/// (input token, output token, max splits): split and single-route quotes price differently.
pub type PairKey = (Address, Address, usize);
use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

/// Recent quotes of a similar size: within this factor of the amount, either way.
const SIZE_BAND: u128 = 2;
const MAX_SAMPLES: usize = 256;

struct Sample {
    at: Instant,
    amount: u128,
    rate: f64,
}

#[derive(Default)]
struct PairHistory {
    samples: VecDeque<Sample>,
    /// The mirror's full-resync epoch when a quote of this pair was first refused.
    refused_at_full: Option<u64>,
}

#[derive(Debug, PartialEq)]
pub enum Verdict {
    Accept,
    /// Accepted although it deviates: the state was fully re-read after the first refusal.
    Confirmed { deviation_bps: f64 },
    Refuse { deviation_bps: f64, reference: f64 },
}

pub struct Guard {
    /// 0 disables the guard.
    pub max_jump_bps: f64,
    pub window: Duration,
    pairs: parking_lot::Mutex<HashMap<PairKey, PairHistory>>,
}

impl Guard {
    pub fn new(max_jump_bps: f64, window: Duration) -> Self {
        Guard { max_jump_bps, window, pairs: Default::default() }
    }

    /// Records a quote without checking it (warm-up quotes).
    pub fn seed(&self, pair: PairKey, amount: u128, out: u128, now: Instant) {
        if amount > 0 {
            let mut pairs = self.pairs.lock();
            Self::push(pairs.entry(pair).or_default(), amount, out as f64 / amount as f64, now);
        }
    }

    fn push(h: &mut PairHistory, amount: u128, rate: f64, now: Instant) {
        h.samples.push_back(Sample { at: now, amount, rate });
        while h.samples.len() > MAX_SAMPLES {
            h.samples.pop_front();
        }
    }

    /// Checks a quote of `amount` that returned `out`. `full_epoch` is the mirror's current
    /// full-resync epoch.
    pub fn check(&self, pair: PairKey, amount: u128, out: u128, full_epoch: u64, now: Instant) -> Verdict {
        if amount == 0 {
            return Verdict::Accept;
        }
        let rate = out as f64 / amount as f64;
        let mut pairs = self.pairs.lock();
        let h = pairs.entry(pair).or_default();
        while h.samples.front().is_some_and(|s| now.duration_since(s.at) > self.window) {
            h.samples.pop_front();
        }
        let mut near: Vec<f64> = h
            .samples
            .iter()
            .filter(|s| s.amount <= amount.saturating_mul(SIZE_BAND) && amount <= s.amount.saturating_mul(SIZE_BAND))
            .map(|s| s.rate)
            .collect();
        if self.max_jump_bps <= 0.0 || near.is_empty() {
            Self::push(h, amount, rate, now);
            return Verdict::Accept;
        }
        near.sort_by(|a, b| a.total_cmp(b));
        let reference = near[near.len() / 2];
        let deviation_bps = (rate / reference - 1.0) * 1e4;
        if deviation_bps.abs() <= self.max_jump_bps {
            h.refused_at_full = None;
            Self::push(h, amount, rate, now);
            return Verdict::Accept;
        }
        match h.refused_at_full {
            Some(e) if full_epoch > e => {
                h.samples.clear();
                h.refused_at_full = None;
                Self::push(h, amount, rate, now);
                Verdict::Confirmed { deviation_bps }
            }
            Some(_) => Verdict::Refuse { deviation_bps, reference },
            None => {
                h.refused_at_full = Some(full_epoch);
                Verdict::Refuse { deviation_bps, reference }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const P: PairKey = (Address::ZERO, Address::repeat_byte(1), 4);

    #[test]
    fn refuses_the_2026_10_19_misquotes_until_a_full_resync_confirms() {
        let g = Guard::new(200.0, Duration::from_secs(600));
        let t = Instant::now();
        // Sane PYUSD->USDG quotes from the dry run (6 decimals).
        g.seed(P, 69_999_106_722, 69_994_085_357, t);
        assert_eq!(g.check(P, 69_999_106_722, 69_994_085_357, 1, t), Verdict::Accept);
        // 61,498.474155 PYUSD -> 69,998.471799 USDG.
        let v = g.check(P, 61_498_474_155, 69_998_471_799, 1, t);
        let Verdict::Refuse { deviation_bps, .. } = v else { panic!("{v:?}") };
        assert!((deviation_bps - 1382.0).abs() < 2.0, "{deviation_bps}");
        // Still refused until the state has been re-read in full.
        assert!(matches!(g.check(P, 61_498_476_813, 69_998_473_132, 1, t), Verdict::Refuse { .. }));
        // After the full re-read the sane quote is back and accepted.
        assert_eq!(g.check(P, 61_498_474_155, 61_493_590_000, 2, t), Verdict::Accept);
    }

    #[test]
    fn a_move_that_survives_a_full_resync_becomes_the_reference() {
        let g = Guard::new(200.0, Duration::from_secs(600));
        let t = Instant::now();
        g.seed(P, 1_000_000, 1_000_000, t);
        assert!(matches!(g.check(P, 1_000_000, 960_000, 7, t), Verdict::Refuse { .. }));
        assert!(matches!(g.check(P, 1_000_000, 960_000, 8, t), Verdict::Confirmed { .. }));
        assert_eq!(g.check(P, 1_000_000, 961_000, 8, t), Verdict::Accept);
    }

    #[test]
    fn compares_only_similar_sizes_and_recent_quotes() {
        let g = Guard::new(200.0, Duration::from_secs(60));
        let t = Instant::now();
        // A 1M quote through thin pools prices 5% worse than a 1k one: not comparable.
        g.seed(P, 1_000_000_000, 950_000_000, t);
        assert_eq!(g.check(P, 1_000_000, 1_000_000, 0, t), Verdict::Accept);
        // Samples older than the window are dropped.
        let later = t + Duration::from_secs(120);
        assert_eq!(g.check(P, 1_000_000_000, 1_000_000_000, 0, later), Verdict::Accept);
    }

    #[test]
    fn disabled_at_zero() {
        let g = Guard::new(0.0, Duration::from_secs(60));
        let t = Instant::now();
        g.seed(P, 1_000_000, 1_000_000, t);
        assert_eq!(g.check(P, 1_000_000, 2_000_000, 0, t), Verdict::Accept);
    }
}
