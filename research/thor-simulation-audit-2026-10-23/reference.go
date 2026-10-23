// Offline oracle translated from THORNode v3.20.3 swap_current.go,
// types/msg_swap.go, common/type_convert.go and types/type_pool.go.
// Uses Go math/big independently of the Python simulator. No RPC or signing.
package main

import (
	"encoding/json"
	"fmt"
	"math/big"
	"os"
)

func product(xs ...int64) *big.Int {
	n := big.NewInt(1)
	for _, x := range xs { n.Mul(n, big.NewInt(x)) }
	return n
}
func ratio(n *big.Int, d int64, rounding string) int64 {
	q, r := new(big.Int), new(big.Int)
	q.QuoRem(n, big.NewInt(d), r)
	cmp := new(big.Int).Lsh(r, 1).Cmp(big.NewInt(d))
	if rounding == "up" && cmp >= 0 || rounding == "even" && (cmp > 0 || cmp == 0 && q.Bit(0) == 1) { q.Add(q,big.NewInt(1)) }
	return q.Int64()
}
func share(p,t,a int64) int64 {
	if p <= 0 || t <= 0 || a <= 0 { return 0 }
	if p > t { p = t }
	return ratio(product(p,a),t,"up")
}
func leg(X,x,Y,floor int64) (int64,int64) {
	slipDec := ratio(product(x,1000000000000000000),X+x,"even")
	slip := ratio(product(slipDec,10000),1000000000000000000,"even")
	if floor > slip {
		// The intermediate allocation x*Y can exceed int64.
		n := product(floor,x,Y); n.Add(n,big.NewInt(5000)); n.Quo(n,big.NewInt(10000))
		fee := n.Quo(n,big.NewInt(X+x)).Int64()
		return ratio(product(x,Y),X+x,"floor")-fee,fee
	}
	d := product(X+x,X+x)
	return new(big.Int).Quo(product(x,X,Y),d).Int64(),new(big.Int).Quo(product(x,x,Y),d).Int64()
}

type Trace struct { Size, Target, Gross, FeeShare, SourceRune, TargetRune int64; Filled bool }
type Result struct { Filled, AfterAffiliate int64; Trace []Trace }
func simulate(Xa,Xr,Ya,Yr,deposit,quantity,target,outbound,aff int64) Result {
	r := Result{}
	gross := int64(0)
	for count := int64(0); count < quantity; count++ {
		size := deposit/quantity
		if count < deposit%quantity { size++ }
		if size > deposit-r.Filled { size = deposit-r.Filled }
		if size == 0 { break }
		remainingTarget := target-gross; if remainingTarget < 0 { remainingTarget = 0 }
		limit := share(size,deposit-r.Filled,remainingTarget)
		rune,sourceFee := leg(Xa,size,Xr,2); rune = rune/100*100
		emit,targetFee := leg(Yr,rune,Ya,5)
		netCheck := emit-outbound/quantity; if netCheck < 0 { netCheck = 0 }
		ok := emit > 0 && (limit == 0 || netCheck >= limit)
		r.Trace = append(r.Trace,Trace{size,limit,emit,outbound/quantity,Xr,Yr,ok})
		if !ok { break }
		Xa += size; Xr -= rune+sourceFee
		Yr += rune; Ya -= emit
		Yr -= ratio(product(Yr,targetFee),Ya,"up")
		r.Filled += size; gross += emit
	}
	r.AfterAffiliate = gross-share(aff,10000,gross)
	return r
}
func main() {
	Xa,Xr,Ya,Yr := int64(232063647963774),int64(360079374667876),int64(6079800776242),int64(9411627687988)
	deposit,target,outbound := int64(9996400000000),int64(9998399280000),int64(237317259)
	results := map[string]Result{}
	for name,q := range map[string]int64{"rapid":3296,"maximum_capacity_quantity":1743,"resting":720} {
		results[name] = simulate(Xa,Xr,Ya,Yr,deposit,q,target,outbound,1)
	}
	// A deeper premium exercises many fee-removal updates, not just the first slice.
	results["multi_fill"] = simulate(Xa,Xr,Ya*103/100,Yr,deposit,3296,target,outbound,1)
	b,_ := json.MarshalIndent(results,"","  ")
	if len(os.Args)>1 { if err:=os.WriteFile(os.Args[1],b,0644);err!=nil { panic(err) } }
	fmt.Println(string(b))
}
