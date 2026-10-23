package thorchain

import (
	"fmt"
	"strconv"
	"strings"

	"gitlab.com/thorchain/thornode/v3/common"
	"gitlab.com/thorchain/thornode/v3/common/cosmos"
	"gitlab.com/thorchain/thornode/v3/constants"
	"gitlab.com/thorchain/thornode/v3/x/thorchain/keeper"
)

// SwapQueue is going to manage the swaps queue
type SwapQueueImpl struct {
	k       keeper.Keeper
	handler func(mgr Manager) cosmos.Handler
}

// newSwapQueue create a new vault manager
func newSwapQueue(k keeper.Keeper) *SwapQueueImpl {
	return &SwapQueueImpl{
		k:       k,
		handler: NewInternalHandler,
	}
}

// FetchQueue - grabs all swap queue items from the kvstore and returns them
func (vm *SwapQueueImpl) FetchQueue(ctx cosmos.Context) (swapItems, error) { // nolint
	items := make(swapItems, 0)
	iterator := vm.k.GetSwapQueueIterator(ctx)
	defer iterator.Close()
	// Collect keys for malformed items that cannot be processed, so they can
	// be removed after iteration completes. Without this, a corrupted entry
	// would be re-encountered every block forever, never executed and never
	// refunded.
	var malformedKeys [][]byte
	for ; iterator.Valid(); iterator.Next() {
		var msg MsgSwap
		if err := vm.k.Cdc().Unmarshal(iterator.Value(), &msg); err != nil {
			ctx.Logger().Error("fail to fetch swap msg from queue, removing malformed item", "key", iterator.Key(), "error", err)
			key := append([]byte(nil), iterator.Key()...)
			malformedKeys = append(malformedKeys, key)
			continue
		}

		ss := strings.Split(string(iterator.Key()), "-")
		i, err := strconv.Atoi(ss[len(ss)-1])
		if err != nil {
			ctx.Logger().Error("fail to parse swap queue msg index, removing malformed item", "key", iterator.Key(), "error", err)
			key := append([]byte(nil), iterator.Key()...)
			malformedKeys = append(malformedKeys, key)
			continue
		}

		// exclude streaming swaps when its not "their time". Always want to
		// allow the first sub-swap immediately (ie no LastHeight yet)
		if msg.IsLegacyStreaming() {
			pausedStreaming := vm.k.GetConfigInt64(ctx, constants.StreamingSwapPause)
			if pausedStreaming > 0 {
				continue
			}
			swp := msg.GetStreamingSwap()
			if vm.k.StreamingSwapExists(ctx, msg.Tx.ID) {
				var err error
				swp, err = vm.k.GetStreamingSwap(ctx, msg.Tx.ID)
				if err != nil {
					ctx.Logger().Error("fail to fetch streaming swap", "error", err)
					continue
				}
			}
			if swp.LastHeight > 0 { // if we don't have a height, do first swap attempt now
				if swp.LastHeight >= ctx.BlockHeight() {
					// last swap must be in the past
					continue // skip
				}
				interval := swp.Interval
				if interval == 0 {
					interval = 1
				}
				if (ctx.BlockHeight()-swp.LastHeight)%int64(interval) != 0 {
					continue // skip
				}
				if vm.k.IsTradingHalt(ctx, &msg) {
					// if trading/chain is halted, skip
					continue // skip
				}
			}
		}

		items = append(items, swapItem{
			msg:   msg,
			index: i,
			fee:   cosmos.ZeroUint(),
		})
	}

	for _, key := range malformedKeys {
		vm.k.DeleteKey(ctx, key)
	}

	return items, nil
}

// EndBlock trigger the real swap to be processed
func (vm *SwapQueueImpl) EndBlock(ctx cosmos.Context, mgr Manager) error {
	handler := vm.handler(mgr)

	minSwapsPerBlock, err := vm.k.GetMimir(ctx, constants.MinSwapsPerBlock.String())
	if minSwapsPerBlock < 0 || err != nil {
		minSwapsPerBlock = mgr.GetConstants().GetInt64Value(constants.MinSwapsPerBlock)
	}
	maxSwapsPerBlock, err := vm.k.GetMimir(ctx, constants.MaxSwapsPerBlock.String())
	if maxSwapsPerBlock < 0 || err != nil {
		maxSwapsPerBlock = mgr.GetConstants().GetInt64Value(constants.MaxSwapsPerBlock)
	}
	synthVirtualDepthMult, err := vm.k.GetMimir(ctx, constants.VirtualMultSynthsBasisPoints.String())
	if synthVirtualDepthMult < 1 || err != nil {
		synthVirtualDepthMult = mgr.GetConstants().GetInt64Value(constants.VirtualMultSynthsBasisPoints)
	}

	swaps, err := vm.FetchQueue(ctx)
	if err != nil {
		ctx.Logger().Error("fail to fetch swap queue from store", "error", err)
		return err
	}
	swaps, err = vm.scoreMsgs(ctx, swaps, synthVirtualDepthMult)
	if err != nil {
		ctx.Logger().Error("fail to fetch swap items", "error", err)
		// continue, don't exit, just do them out of order (instead of not at all)
	}
	swaps = swaps.Sort()

	for i := int64(0); i < vm.getTodoNum(int64(len(swaps)), minSwapsPerBlock, maxSwapsPerBlock); i++ {
		pick := swaps[i]
		// grab swp BEFORE a streaming swap modified the msg.Tx.Coins[0].Amount
		// value. This is used later to refund the correct amount
		swp := pick.msg.GetStreamingSwap()

		triggerRefund := false
		_, handleErr := handler(ctx, &pick.msg)
		if handleErr != nil {
			ctx.Logger().Error("fail to swap", "msg", pick.msg.Tx.String(), "error", handleErr)

			var refundErr error
			triggerRefund = !pick.msg.IsLegacyStreaming()

			if pick.msg.IsLegacyStreaming() {
				if vm.k.StreamingSwapExists(ctx, pick.msg.Tx.ID) {
					var getErr error
					swp, getErr = vm.k.GetStreamingSwap(ctx, pick.msg.Tx.ID)
					if getErr != nil {
						ctx.Logger().Error("fail to fetch streaming swap", "error", getErr)
						return getErr
					}
				}

				// if we haven't made any swaps yet, its safe to do a regular
				// refund. Otherwise allow later code to do partial refunds
				triggerRefund = swp.In.IsZero() && swp.Out.IsZero()
				if triggerRefund {
					// revert the tx amount to the be original deposit amount
					pick.msg.Tx.Coins[0].Amount = swp.Deposit
					vm.k.RemoveStreamingSwap(ctx, pick.msg.Tx.ID)
					vm.k.RemoveSwapQueueItem(ctx, pick.msg.Tx.ID, pick.index)
				}
			} else {
				// Should not refund a failed preferred asset swap, the RUNE is still in the AffiliateCollector module
				var affColAddress common.Address
				affColAddress, err = mgr.Keeper().GetModuleAddress(AffiliateCollectorName)
				if err != nil {
					ctx.Logger().Error("failed to retrieve AffiliateCollector module address", "error", err)
				}

				if strings.HasPrefix(pick.msg.Tx.Memo, PreferredAssetSwapMemoPrefix) && pick.msg.Tx.FromAddress.Equals(affColAddress) {
					triggerRefund = false
					// clean up failed preferred asset swap
					runeAmt := pick.msg.Tx.Coins[0].Amount
					memo := pick.msg.Tx.Memo
					if err = vm.cleanupFailedPreferredAssetSwap(ctx, mgr, memo, runeAmt); err != nil {
						ctx.Logger().Error("failed to cleanup failed preferred asset swap", "error", err)
					}
				}
			}

			if triggerRefund {
				// Get the full ObservedTx from the TxID, for the vault ObservedPubKey to first try to refund from.
				voter, voterErr := mgr.Keeper().GetObservedTxInVoter(ctx, pick.msg.Tx.ID)
				if voterErr == nil && !voter.Tx.IsEmpty() {
					refundErr = refundTx(ctx, ObservedTx{Tx: pick.msg.Tx, ObservedPubKey: voter.Tx.ObservedPubKey}, mgr, CodeSwapFail, handleErr.Error(), "")
				} else {
					// If the full ObservedTx could not be retrieved, proceed with just the MsgSwap's Tx (no ObservedPubKey).
					ctx.Logger().Error("fail to get non-empty observed tx", "error", voterErr)
					refundErr = refundTx(ctx, ObservedTx{Tx: pick.msg.Tx}, mgr, CodeSwapFail, handleErr.Error(), "")
				}

				if refundErr != nil {
					ctx.Logger().Error("fail to refund swap", "error", refundErr)
				}
			}
		}

		// Only track streaming swap state for actual streaming swaps (not limit swaps)
		// Limit swaps with streaming parameters should execute once and complete,
		// not persist state across blocks which causes the hollowed-out record bug.
		if pick.msg.IsLegacyStreaming() && !pick.msg.IsLimitSwap() {
			swp, err = vm.k.GetStreamingSwap(ctx, pick.msg.Tx.ID)
			if err != nil {
				ctx.Logger().Error("fail to fetch streaming swap", "error", err)
				return err
			}
			swp.Count += 1
			if handleErr != nil {
				swp.FailedSwaps = append(swp.FailedSwaps, swp.Count)
				swp.FailedSwapReasons = append(swp.FailedSwapReasons, handleErr.Error())
			}
			swp.LastHeight = ctx.BlockHeight()
			if !triggerRefund {
				mgr.Keeper().SetStreamingSwap(ctx, swp)
			}
			if swp.Valid() == nil && swp.IsDone() {
				vm.k.RemoveSwapQueueItem(ctx, pick.msg.Tx.ID, pick.index)
				vm.k.RemoveStreamingSwap(ctx, pick.msg.Tx.ID)

				// The streaming swap and queue state have now been removed, so this
				// item will never be retried. Any fallible operation below must
				// therefore never return without first dispatching the already-swapped
				// output (swp.Out); otherwise those funds are permanently stranded in
				// the pool/vault with no outbound and no refund. The memo is only
				// re-parsed here to detect an add (a savers add skips the outbound).
				// A full re-parse can now fail even for an originally-valid memo (e.g.
				// an affiliate/destination THORName expired mid-stream), so on failure
				// fall back to the memo type, which is derived from the memo prefix and
				// does not depend on THORName resolution. This keeps the add/non-add
				// decision identical to a successful parse instead of returning and
				// stranding the output.
				isSaversAdd := false
				if memo, memoErr := ParseMemoWithTHORNames(ctx, vm.k, pick.msg.Tx.Memo); memoErr == nil {
					isSaversAdd = memo.IsType(TxAdd)
				} else if txType, typeErr := StringToTxType(strings.Split(strings.Split(pick.msg.Tx.Memo, "|")[0], ":")[0]); typeErr == nil {
					isSaversAdd = txType == TxAdd
					ctx.Logger().Error("fail to parse streaming swap memo, using memo type for outbound decision", "error", memoErr, "hash", pick.msg.Tx.ID, "is_add", isSaversAdd)
				} else {
					ctx.Logger().Error("fail to parse streaming swap memo and memo type, scheduling outbound as non-add", "error", memoErr, "hash", pick.msg.Tx.ID)
				}

				// If this is a savers add skip scheduling outbound
				if !swp.Out.IsZero() && !isSaversAdd {
					dexAgg := ""
					if len(pick.msg.Aggregator) > 0 {
						// A failed aggregator lookup (e.g. the aggregator was removed
						// from the whitelist across an upgrade while the streaming swap
						// was in flight) must not strand the already-swapped output;
						// log and schedule a plain outbound without the aggregator.
						aggregator, aggErr := FetchDexAggregator(
							pick.msg.TargetAsset.GetChain(),
							pick.msg.Aggregator,
						)
						if aggErr != nil {
							ctx.Logger().Error("fail to fetch dex aggregator, scheduling outbound without aggregator", "error", aggErr, "hash", pick.msg.Tx.ID)
						} else {
							dexAgg = aggregator
						}
					}
					dexAggTargetAsset := pick.msg.AggregatorTargetAddress

					toi := TxOutItem{
						Chain:                 pick.msg.TargetAsset.GetChain(),
						InHash:                pick.msg.Tx.ID,
						ToAddress:             pick.msg.Destination,
						Coin:                  common.NewCoin(pick.msg.TargetAsset, swp.Out),
						Aggregator:            dexAgg,
						AggregatorTargetAsset: dexAggTargetAsset,
						AggregatorTargetLimit: pick.msg.AggregatorTargetLimit,
					}

					if _, err := mgr.TxOutStore().TryAddTxOutItem(ctx, mgr, toi, cosmos.ZeroUint()); err != nil {
						ctx.Logger().Error("fail streaming swap outbound", "error", err)
						unrefundableCoinCleanup(ctx, mgr, toi, "failed_outbound")

						// Emit a "fail to refund" refund event to signal to explorers/interfaces what has happened to the streaming swap output.
						refundReason := fmt.Sprintf("%s; fail to refund (%s): streaming swap output", err, toi.Coin.String())
						// All aspects of the inbound Tx are unchanged except for the Coins, which here have become the already-swapped output Coins.
						refundTx := common.NewTx(pick.msg.Tx.ID, pick.msg.Tx.FromAddress, pick.msg.Tx.ToAddress, common.NewCoins(toi.Coin), pick.msg.Tx.Gas, pick.msg.Tx.Memo)
						eventRefund := NewEventRefund(CodeFailAddOutboundTx, refundReason, refundTx, common.Fee{}) // fee param not used in downstream event
						if err := mgr.EventMgr().EmitEvent(ctx, eventRefund); err != nil {
							ctx.Logger().Error("fail to emit refund event", "error", err)
						}
					}
				}

				if swp.Deposit.GT(swp.In) {
					remainder := common.SafeSub(swp.Deposit, swp.In)
					source := pick.msg.Tx.Coins[0].Asset
					refundCoin := common.NewCoin(source, remainder)
					refundCoinTx := pick.msg.Tx
					refundCoinTx.Coins = common.NewCoins(refundCoin)
					// As this is a streaming swap's partial refund, the vault context may have changed, so do vault selection.
					if refundErr := refundTx(ctx, ObservedTx{Tx: refundCoinTx}, mgr, CodeSwapFail, "streaming partial-refund", ""); refundErr != nil {
						ctx.Logger().Error("fail to partial-refund swap", "error", refundErr)
					}
				}

				evt := NewEventStreamingSwap(pick.msg.Tx.Coins[0].Asset, pick.msg.TargetAsset, swp)
				if err := mgr.EventMgr().EmitEvent(ctx, evt); err != nil {
					ctx.Logger().Error("fail to emit streaming swap event", "error", err)
				}
			}
		} else {
			vm.k.RemoveSwapQueueItem(ctx, pick.msg.Tx.ID, pick.index)
		}
	}
	return nil
}

func (vm *SwapQueueImpl) cleanupFailedPreferredAssetSwap(ctx cosmos.Context, mgr Manager, memo string, runeAmt cosmos.Uint) error {
	ctx.Logger().Info("preferred asset swap failed, send rune back to affiliate collector", "runeAmt", runeAmt.String(), "memo", memo)
	// get the preferred asset swap's thorname
	name, ok := strings.CutPrefix(memo, fmt.Sprintf("%s-", PreferredAssetSwapMemoPrefix))
	if !ok {
		return fmt.Errorf("failed to get thorname from memo: %s", memo)
	}
	if tn, err := vm.k.GetTHORName(ctx, name); err == nil {
		var affCol AffiliateFeeCollector
		affCol, err = mgr.Keeper().GetAffiliateCollector(ctx, tn.Owner)
		if err != nil {
			return fmt.Errorf("failed to get affiliate collector record: %w", err)
		} else {
			affCol.RuneAmount = affCol.RuneAmount.Add(runeAmt)
			mgr.Keeper().SetAffiliateCollector(ctx, affCol)
		}
	} else {
		return fmt.Errorf("failed to get thorname: %w", err)
	}

	// send rune back to affiliate collector
	if err := mgr.Keeper().SendFromModuleToModule(ctx, AsgardName, AffiliateCollectorName, common.NewCoins(common.NewCoin(common.RuneAsset(), runeAmt))); err != nil {
		return fmt.Errorf("failed to send rune back to affiliate collector: %w", err)
	}

	return nil
}

// getTodoNum - determine how many swaps to do.
func (vm *SwapQueueImpl) getTodoNum(queueLen, minSwapsPerBlock, maxSwapsPerBlock int64) int64 {
	// Do half the length of the queue. Unless...
	//  1. Half the queue length is greater than maxSwapsPerBlock
	//  2. Half the queue length is less than minSwapsPerBlock
	todo := queueLen / 2
	if minSwapsPerBlock > todo {
		if queueLen < minSwapsPerBlock {
			todo = queueLen
		} else {
			todo = minSwapsPerBlock
		}
	}
	if maxSwapsPerBlock < todo {
		todo = maxSwapsPerBlock
	}
	return todo
}

// scoreMsgs - this takes a list of MsgSwap, and converts them to a scored
// swapItem list
func (vm *SwapQueueImpl) scoreMsgs(ctx cosmos.Context, items swapItems, synthVirtualDepthMult int64) (swapItems, error) {
	pools := make(map[common.Asset]Pool)

	for i, item := range items {
		// the asset customer send
		sourceAsset := item.msg.Tx.Coins[0].Asset
		// the asset customer want
		targetAsset := item.msg.TargetAsset

		for _, a := range []common.Asset{sourceAsset, targetAsset} {
			if a.IsRune() {
				continue
			}

			if _, ok := pools[a]; !ok {
				var err error
				pools[a], err = vm.k.GetPool(ctx, a.GetLayer1Asset())
				if err != nil {
					ctx.Logger().Error("fail to get pool", "pool", a, "error", err)
					continue
				}
			}
		}

		isDoubleSwap := !sourceAsset.IsRune() && !targetAsset.IsRune()
		stableSwap := isDoubleSwap && isStableToStable(ctx, vm.k, sourceAsset, targetAsset)

		nonRuneAsset := sourceAsset
		if nonRuneAsset.IsRune() {
			nonRuneAsset = targetAsset
		}
		pool := pools[nonRuneAsset]
		if pool.IsEmpty() || pool.BalanceRune.IsZero() || pool.BalanceAsset.IsZero() {
			continue
		}
		// synths may be redeemed on unavailable pools, score them
		if !pool.IsAvailable() && !sourceAsset.IsSyntheticAsset() {
			continue
		}
		virtualDepthMult := int64(10_000)
		if nonRuneAsset.IsSyntheticAsset() {
			virtualDepthMult = synthVirtualDepthMult
		}
		// first-leg minSlip asset matches swapOne: non-rune side of this leg.
		emitRune := vm.addLiquidityFee(ctx, pool, item.msg.Tx.Coins[0], &items[i], virtualDepthMult, nonRuneAsset, stableSwap)

		if !isDoubleSwap {
			continue
		}
		// double swap , thus use the first leg's emitted RUNE and calculate fee again
		runeCoin := common.NewCoin(common.RuneAsset(), emitRune)
		nonRuneAsset = targetAsset
		pool = pools[nonRuneAsset]
		if pool.IsEmpty() || !pool.IsAvailable() || pool.BalanceRune.IsZero() || pool.BalanceAsset.IsZero() {
			// Reset first-leg scoring to prevent inflated priority from partial scoring
			items[i].fee = cosmos.ZeroUint()
			continue
		}
		virtualDepthMult = int64(10_000)
		if targetAsset.IsSyntheticAsset() {
			virtualDepthMult = synthVirtualDepthMult
		}
		vm.addLiquidityFee(ctx, pool, runeCoin, &items[i], virtualDepthMult, nonRuneAsset, stableSwap)
	}

	return items, nil
}

// addLiquidityFee adds the effective liquidity fee (in RUNE) for one swap leg
// to item.fee and returns the emitted amount for the leg. Uses GetSwapCalc so
// the score honors the min-slip floor that swapOne actually charges; without
// it, small swaps whose natural slip is below the floor would score as
// near-zero fee and get deprioritized even though they pay the floor rate in
// practice.
func (vm *SwapQueueImpl) addLiquidityFee(ctx cosmos.Context, pool Pool, sourceCoin common.Coin, item *swapItem, virtualDepthMult int64, nonRuneAsset common.Asset, stableSwap bool) cosmos.Uint {
	// Get our X, x, Y values
	var X, x, Y cosmos.Uint
	x = sourceCoin.Amount
	if sourceCoin.IsRune() {
		X = pool.BalanceRune
		Y = pool.BalanceAsset
	} else {
		Y = pool.BalanceRune
		X = pool.BalanceAsset
	}

	X = common.GetUncappedShare(cosmos.NewUint(uint64(virtualDepthMult)), cosmos.NewUint(10_000), X)
	Y = common.GetUncappedShare(cosmos.NewUint(uint64(virtualDepthMult)), cosmos.NewUint(10_000), Y)

	swapper, err := GetSwapper(vm.k.GetVersion())
	if err != nil {
		panic(err)
	}

	swapSlipBps := swapper.CalcSwapSlip(X, x)
	minSlipBps := getMinSlipBps(ctx, vm.k, common.NoAddress, nonRuneAsset, stableSwap, nil, nil, common.EmptyAsset, common.EmptyAsset, nil)
	emit, fee, _ := swapper.GetSwapCalc(X, x, Y, swapSlipBps, minSlipBps)
	emit = cosmos.RoundToDecimal(emit, pool.Decimals)
	if sourceCoin.IsRune() {
		fee = pool.AssetValueInRune(fee)
	}
	item.fee = item.fee.Add(fee)
	return emit
}
