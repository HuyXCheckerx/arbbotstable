import { useState, useEffect } from "react";
import {
  ArrowRightLeft,
  TrendingUp,
  FileText,
  Wallet,
  CheckCircle2,
  RefreshCw,
  Coins,
  Scale,
  Sparkles,
  Lock,
  Download,
  Search
} from "lucide-react";
import confetti from "canvas-confetti";
import {
  calculateVietnamCryptoTax,
  formatVND,
} from "./services/taxService";
import type { CryptoTransaction, CostBasisMethod } from "./services/taxService";
import { verifyCapToken, requestCapChallenge, redeemCapSolutions } from "./services/capService";

// Mock Sample Transactions for Tax Calculator
const SAMPLE_TRANSACTIONS: CryptoTransaction[] = [
  {
    id: "tx-1",
    timestamp: "2026-02-10T08:30:00Z",
    txHash: "0x89ab...3f12",
    type: "swap",
    assetIn: "USDT",
    amountIn: 5000,
    priceInVND: 25420,
    assetOut: "ETH",
    amountOut: 1.85,
    priceOutVND: 68700000,
    feeInVND: 50000,
  },
  {
    id: "tx-2",
    timestamp: "2026-05-18T14:15:00Z",
    txHash: "0x44cd...91e5",
    type: "swap",
    assetIn: "ETH",
    amountIn: 1.85,
    priceInVND: 79200000,
    assetOut: "VNDL",
    amountOut: 146520000,
    priceOutVND: 1,
    feeInVND: 45000,
  },
  {
    id: "tx-3",
    timestamp: "2026-07-22T10:00:00Z",
    txHash: "0x67ef...22a4",
    type: "lending_yield",
    assetIn: "USDT",
    amountIn: 850,
    priceInVND: 25480,
    assetOut: "VNDL",
    amountOut: 21658000,
    priceOutVND: 1,
    feeInVND: 0,
  },
];

export default function App() {
  // Navigation & Page State
  const [currentPage, setCurrentPage] = useState<"home" | "tax" | "404" | "thankyou">("home");
  const [activeTab, setActiveTab] = useState<"swap" | "borrow" | "lend">("swap");
  const [walletConnected, setWalletConnected] = useState(false);
  const [walletAddress, setWalletAddress] = useState<string | null>(null);

  // 1:1 Stablecoin Swap State
  const [swapFrom, setSwapFrom] = useState<"USDT" | "USDC" | "PYUSD" | "USDG">("USDT");
  const [swapTo, setSwapTo] = useState<"USDT" | "USDC" | "PYUSD" | "USDG">("USDC");
  const [swapAmount, setSwapAmount] = useState<string>("1000");

  // VNDL Borrow State
  const [collateralType, setCollateralType] = useState<"USDT" | "XAUt">("USDT");
  const [collateralAmount, setCollateralAmount] = useState<string>("5000");
  const [borrowVNDAmount, setBorrowVNDAmount] = useState<string>("90000000");

  // Tax Service State
  const [taxMethod, setTaxMethod] = useState<CostBasisMethod>("fifo");
  const [taxReport, setTaxReport] = useState(() =>
    calculateVietnamCryptoTax(SAMPLE_TRANSACTIONS, "fifo", 2026)
  );

  // Cap CAPTCHA & Transaction Modal State
  const [isTxModalOpen, setIsTxModalOpen] = useState(false);
  const [capSolved, setCapSolved] = useState(false);
  const [txSubmitting, setTxSubmitting] = useState(false);
  const [lastTxHash, setLastTxHash] = useState<string | null>(null);

  // Realtime Live Rates (Mocked Oracle feed)
  const USD_VND_RATE = 25460;
  const XAUT_USD_RATE = 2680; // Tether Gold per oz

  // Update Tax Report when method changes
  useEffect(() => {
    setTaxReport(calculateVietnamCryptoTax(SAMPLE_TRANSACTIONS, taxMethod, 2026));
  }, [taxMethod]);

  const handleConnectWallet = () => {
    if (!walletConnected) {
      setWalletAddress("0x71C...4e9A");
      setWalletConnected(true);
    } else {
      setWalletConnected(false);
      setWalletAddress(null);
    }
  };

  const [capToken, setCapToken] = useState<string | null>(null);

  // Execute Swap / Borrow Flow with Cap CAPTCHA verification
  const handleInitiateAction = () => {
    setCapSolved(false);
    setCapToken(null);
    setIsTxModalOpen(true);
  };

  const handleSimulateCapSolve = async () => {
    // Generate valid Cap challenge & simulate client proof-of-work
    const challenge = await requestCapChallenge("vietdefi_swap");
    const redeemRes = await redeemCapSolutions(challenge.token, [42, 108], undefined, "vietdefi_swap");
    if (redeemRes.success && redeemRes.token) {
      setCapToken(redeemRes.token);
      setCapSolved(true);
    }
  };

  const handleConfirmTransaction = async () => {
    if (!capSolved || !capToken) return;
    setTxSubmitting(true);

    // Replay-protected server-side check (fail-closed)
    const isValid = await verifyCapToken(capToken);
    if (!isValid) {
      alert("Xác thực CAPTCHA thất bại hoặc token đã được sử dụng!");
      setTxSubmitting(false);
      return;
    }

    setTimeout(() => {
      setTxSubmitting(false);
      setIsTxModalOpen(false);
      const generatedHash = "0x" + Array.from({ length: 64 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
      setLastTxHash(generatedHash);
      setCurrentPage("thankyou");

      // Celebrate with confetti
      confetti({
        particleCount: 100,
        spread: 70,
        origin: { y: 0.6 },
        colors: ["#00f2fe", "#4facfe", "#10b981", "#f59e0b"],
      });
    }, 1200);
  };

  return (
    <div>
      {/* Navigation Header */}
      <header className="navbar">
        <div className="app-container navbar-inner">
          <a
            href="#home"
            className="brand-logo"
            onClick={(e) => {
              e.preventDefault();
              setCurrentPage("home");
            }}
          >
            <div style={{
              width: "36px",
              height: "36px",
              borderRadius: "10px",
              background: "linear-gradient(135deg, #00f2fe, #4facfe)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "#050811",
              fontWeight: "bold",
              fontSize: "1.2rem"
            }}>
              ₫
            </div>
            <span>VND<span className="brand-gradient">-X</span></span>
          </a>

          <nav>
            <ul className="nav-links">
              <li>
                <button
                  className={`nav-link ${currentPage === "home" && activeTab === "swap" ? "active" : ""}`}
                  onClick={() => { setCurrentPage("home"); setActiveTab("swap"); }}
                  style={{ background: "transparent", border: "none" }}
                >
                  <ArrowRightLeft size={16} /> Hoán Đổi 1:1
                </button>
              </li>
              <li>
                <button
                  className={`nav-link ${currentPage === "home" && activeTab === "borrow" ? "active" : ""}`}
                  onClick={() => { setCurrentPage("home"); setActiveTab("borrow"); }}
                  style={{ background: "transparent", border: "none" }}
                >
                  <Coins size={16} /> Đúc VNDL (CDP)
                </button>
              </li>
              <li>
                <button
                  className={`nav-link ${currentPage === "home" && activeTab === "lend" ? "active" : ""}`}
                  onClick={() => { setCurrentPage("home"); setActiveTab("lend"); }}
                  style={{ background: "transparent", border: "none" }}
                >
                  <TrendingUp size={16} /> Vay & Gửi VND
                </button>
              </li>
              <li>
                <button
                  className={`nav-link ${currentPage === "tax" ? "active" : ""}`}
                  onClick={() => setCurrentPage("tax")}
                  style={{ background: "transparent", border: "none" }}
                >
                  <FileText size={16} /> Thuế Crypto (NQ 05)
                </button>
              </li>
              <li>
                <button
                  className={`nav-link ${currentPage === "404" ? "active" : ""}`}
                  onClick={() => setCurrentPage("404")}
                  style={{ background: "transparent", border: "none" }}
                >
                  Trang 404
                </button>
              </li>
            </ul>
          </nav>

          <div className="nav-actions">
            <span className="badge badge-emerald" style={{ display: "none" }}>
              <span style={{ width: "6px", height: "6px", borderRadius: "50%", background: "#10b981" }}></span>
              Base L2
            </span>
            <button
              className={walletConnected ? "btn btn-secondary btn-sm" : "btn btn-primary btn-sm"}
              onClick={handleConnectWallet}
            >
              <Wallet size={16} />
              {walletConnected ? walletAddress : "Kết Nối Ví"}
            </button>
          </div>
        </div>
      </header>

      {/* Render Current Page */}
      {currentPage === "home" && (
        <main>
          {/* HERO SECTION - CTA ABOVE THE FOLD */}
          <section className="hero-section">
            <div className="app-container hero-grid">
              <div>
                <div className="hero-tag">
                  <Sparkles size={14} /> Tuân thủ Sandbox Nghị Quyết 05/2025/NQ-CP
                </div>
                <h1 className="hero-title">
                  Hạ Tầng Tài Chính Web3 <br />
                  <span className="brand-gradient">&amp; Thuế Crypto Việt Nam</span>
                </h1>
                <p className="hero-description">
                  Hoán đổi Stablecoin 1:1 không trượt giá, đúc VNDL bảo chứng bằng USDT &amp; Vàng XAUt, thị trường cho vay tiền đồng và tự động kê khai thuế TNCN chính xác tuyệt đối.
                </p>

                <div className="hero-cta-group">
                  <button
                    className="btn btn-primary"
                    onClick={() => {
                      document.getElementById("interactive-widget")?.scrollIntoView({ behavior: "smooth" });
                    }}
                  >
                    Bắt Đầu Giao Dịch Ngay
                  </button>
                  <button
                    className="btn btn-secondary"
                    onClick={() => setCurrentPage("tax")}
                  >
                    <FileText size={16} />
                    Tính Thuế Cá Nhân Miễn Phí
                  </button>
                </div>

                <div className="hero-stats">
                  <div className="stat-item">
                    <h4>$148.5M</h4>
                    <p>Khối Lượng Hoán Đổi</p>
                  </div>
                  <div className="stat-item">
                    <h4>0.02%</h4>
                    <p>Phí Cố Định 1:1</p>
                  </div>
                  <div className="stat-item">
                    <h4>10.5%</h4>
                    <p>Lãi Suất Cho Vay VND</p>
                  </div>
                </div>
              </div>

              {/* Interactive Widget Above The Fold */}
              <div id="interactive-widget" className="widget-card">
                <div className="widget-tabs">
                  <button
                    className={`widget-tab ${activeTab === "swap" ? "active" : ""}`}
                    onClick={() => setActiveTab("swap")}
                  >
                    Đổi 1:1 Stable
                  </button>
                  <button
                    className={`widget-tab ${activeTab === "borrow" ? "active" : ""}`}
                    onClick={() => setActiveTab("borrow")}
                  >
                    Đúc VNDL
                  </button>
                  <button
                    className={`widget-tab ${activeTab === "lend" ? "active" : ""}`}
                    onClick={() => setActiveTab("lend")}
                  >
                    Gửi Lãi VND
                  </button>
                </div>

                {/* TAB 1: 1:1 STABLECOIN SWAP */}
                {activeTab === "swap" && (
                  <div>
                    <div className="input-box">
                      <div className="input-header">
                        <span>Bạn Trả</span>
                        <span>Số dư ví: 12,450.00</span>
                      </div>
                      <div className="input-row">
                        <input
                          type="number"
                          className="number-input"
                          value={swapAmount}
                          onChange={(e) => setSwapAmount(e.target.value)}
                        />
                        <select
                          className="token-pill"
                          value={swapFrom}
                          onChange={(e) => setSwapFrom(e.target.value as any)}
                          style={{ background: "transparent", color: "#fff" }}
                        >
                          <option value="USDT">USDT</option>
                          <option value="USDC">USDC</option>
                          <option value="PYUSD">PYUSD</option>
                          <option value="USDG">USDG</option>
                        </select>
                      </div>
                    </div>

                    <div style={{ display: "flex", justifyContent: "center", margin: "-8px 0 8px" }}>
                      <button
                        className="btn btn-secondary btn-sm"
                        style={{ borderRadius: "50%", width: "36px", height: "36px", padding: 0 }}
                        onClick={() => {
                          const temp = swapFrom;
                          setSwapFrom(swapTo);
                          setSwapTo(temp);
                        }}
                      >
                        <RefreshCw size={14} />
                      </button>
                    </div>

                    <div className="input-box">
                      <div className="input-header">
                        <span>Bạn Nhận (Tỷ lệ 1:1)</span>
                        <span>Trượt giá: 0.00%</span>
                      </div>
                      <div className="input-row">
                        <input
                          type="text"
                          className="number-input"
                          readOnly
                          value={(parseFloat(swapAmount || "0") * 0.9998).toFixed(2)}
                        />
                        <select
                          className="token-pill"
                          value={swapTo}
                          onChange={(e) => setSwapTo(e.target.value as any)}
                          style={{ background: "transparent", color: "#fff" }}
                        >
                          <option value="USDC">USDC</option>
                          <option value="USDT">USDT</option>
                          <option value="PYUSD">PYUSD</option>
                          <option value="USDG">USDG</option>
                        </select>
                      </div>
                    </div>

                    <div style={{ fontSize: "0.85rem", color: "var(--text-secondary)", marginBottom: "16px" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "4px" }}>
                        <span>Phí giao dịch giao thức (0.02%):</span>
                        <span>{(parseFloat(swapAmount || "0") * 0.0002).toFixed(2)} {swapFrom}</span>
                      </div>
                      <div style={{ display: "flex", justifyContent: "space-between" }}>
                        <span>Thời gian khớp lệnh:</span>
                        <span style={{ color: "var(--accent-emerald)" }}>Tức thì (~ 1.2s)</span>
                      </div>
                    </div>

                    <button className="btn btn-primary" style={{ width: "100%" }} onClick={handleInitiateAction}>
                      Hoán Đổi {swapFrom} Sang {swapTo}
                    </button>
                  </div>
                )}

                {/* TAB 2: MINT VNDL (OVER-COLLATERALIZED CDP) */}
                {activeTab === "borrow" && (
                  <div>
                    <div className="input-box">
                      <div className="input-header">
                        <span>Tài Sản Thế Chấp</span>
                        <span>Tỷ lệ MCR: {collateralType === "USDT" ? "120%" : "140%"}</span>
                      </div>
                      <div className="input-row">
                        <input
                          type="number"
                          className="number-input"
                          value={collateralAmount}
                          onChange={(e) => {
                            setCollateralAmount(e.target.value);
                            const val = parseFloat(e.target.value || "0");
                            const maxVnd = collateralType === "USDT"
                              ? (val * USD_VND_RATE) / 1.25
                              : (val * XAUT_USD_RATE * USD_VND_RATE) / 1.45;
                            setBorrowVNDAmount(Math.round(maxVnd * 0.8).toString());
                          }}
                        />
                        <select
                          className="token-pill"
                          value={collateralType}
                          onChange={(e) => setCollateralType(e.target.value as any)}
                          style={{ background: "transparent", color: "#fff" }}
                        >
                          <option value="USDT">USDT</option>
                          <option value="XAUt">XAUt (Vàng)</option>
                        </select>
                      </div>
                      <div style={{ fontSize: "0.8rem", color: "var(--text-muted)", marginTop: "6px" }}>
                        Giá trị: {formatVND(
                          (parseFloat(collateralAmount || "0") *
                            (collateralType === "USDT" ? 1 : XAUT_USD_RATE)) * USD_VND_RATE
                        )}
                      </div>
                    </div>

                    <div className="input-box">
                      <div className="input-header">
                        <span>Đúc Tiền Đồng (VNDL)</span>
                        <span>1 VNDL = 1 VNĐ</span>
                      </div>
                      <div className="input-row">
                        <input
                          type="number"
                          className="number-input"
                          value={borrowVNDAmount}
                          onChange={(e) => setBorrowVNDAmount(e.target.value)}
                        />
                        <span className="token-pill">VNDL</span>
                      </div>
                    </div>

                    <div style={{ padding: "12px", background: "rgba(16, 185, 129, 0.1)", borderRadius: "8px", marginBottom: "16px", border: "1px solid rgba(16, 185, 129, 0.25)" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.85rem", marginBottom: "4px" }}>
                        <span>Hệ số an toàn (Health Factor):</span>
                        <span style={{ fontWeight: "bold", color: "var(--accent-emerald)" }}>1.84 (An Toàn)</span>
                      </div>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.85rem" }}>
                        <span>Phí ổn định (APR):</span>
                        <span>2.5% / năm</span>
                      </div>
                    </div>

                    <button className="btn btn-gold" style={{ width: "100%" }} onClick={handleInitiateAction}>
                      Mở Vault &amp; Đúc {formatVND(parseFloat(borrowVNDAmount || "0"))}
                    </button>
                  </div>
                )}

                {/* TAB 3: LEND CRYPTO -> EARN VND INTEREST */}
                {activeTab === "lend" && (
                  <div>
                    <div className="input-box">
                      <div className="input-header">
                        <span>Gửi Tiền Tiết Kiệm Crypto</span>
                        <span>Lãi suất cố định: 9.5% APY</span>
                      </div>
                      <div className="input-row">
                        <input type="number" className="number-input" defaultValue="2000" />
                        <span className="token-pill">USDT</span>
                      </div>
                    </div>

                    <div style={{ background: "rgba(0, 242, 254, 0.08)", padding: "14px", borderRadius: "10px", marginBottom: "18px", border: "1px solid rgba(0, 242, 254, 0.2)" }}>
                      <div style={{ fontSize: "0.85rem", color: "var(--text-secondary)", marginBottom: "6px" }}>
                        Tiền lãi ước tính nhận về mỗi tháng:
                      </div>
                      <div style={{ fontSize: "1.4rem", fontWeight: "700", color: "var(--accent-cyan)", fontFamily: "var(--font-mono)" }}>
                        {formatVND((2000 * USD_VND_RATE * 0.095) / 12)} / tháng
                      </div>
                      <div style={{ fontSize: "0.78rem", color: "var(--text-muted)", marginTop: "4px" }}>
                        Thanh toán trực tiếp vào tài khoản ngân hàng Việt Nam hoặc nhận qua VNDL on-chain.
                      </div>
                    </div>

                    <button className="btn btn-primary" style={{ width: "100%" }} onClick={handleInitiateAction}>
                      Gửi Crypto &amp; Nhận Lãi Tiền Đồng
                    </button>
                  </div>
                )}
              </div>
            </div>
          </section>

          {/* FEATURES SECTION */}
          <section className="section" style={{ background: "rgba(255, 255, 255, 0.01)" }}>
            <div className="app-container">
              <div className="section-header">
                <span className="section-tag">Hệ Sinh Thái Toàn Diện</span>
                <h2 className="section-title">4 Trụ Cột Tài Chính Số Cho Người Việt</h2>
                <p className="section-subtitle">
                  Kết nối thanh khoản toàn cầu với khung pháp lý và tài chính nội địa Việt Nam.
                </p>
              </div>

              <div className="feature-grid">
                <div className="feature-card">
                  <div className="feature-icon-wrapper">
                    <ArrowRightLeft size={24} />
                  </div>
                  <h3 className="feature-title">Hoán Đổi 1:1 Peg Stability Module</h3>
                  <p className="feature-desc">
                    Hoán đổi tỷ lệ 1:1 không trượt giá giữa USDT, USDC, PayPal USD (PYUSD) và Paxos USDG. Phí giao thức chỉ 0.02%, không chịu tác động biến động thị trường.
                  </p>
                  <a
                    href="#swap"
                    className="btn btn-secondary btn-sm"
                    onClick={(e) => { e.preventDefault(); setActiveTab("swap"); }}
                  >
                    Trải nghiệm Swap 1:1
                  </a>
                </div>

                <div className="feature-card">
                  <div className="feature-icon-wrapper gold">
                    <Coins size={24} />
                  </div>
                  <h3 className="feature-title">Đồng Stablecoin VNDL Bảo Chứng Vàng</h3>
                  <p className="feature-desc">
                    VNDL được neo giá 1:1 với Đồng Việt Nam, bảo chứng minh bạch on-chain bằng Tether USD và Vàng XAUt với tỷ lệ thế chấp tối thiểu 120%.
                  </p>
                  <a
                    href="#borrow"
                    className="btn btn-secondary btn-sm"
                    onClick={(e) => { e.preventDefault(); setActiveTab("borrow"); }}
                  >
                    Xem Hợp Đồng Vault
                  </a>
                </div>

                <div className="feature-card">
                  <div className="feature-icon-wrapper emerald">
                    <TrendingUp size={24} />
                  </div>
                  <h3 className="feature-title">Vay Tiền Đồng &amp; Gửi Lãi Tiết Kiệm</h3>
                  <p className="feature-desc">
                    Thế chấp BTC, ETH để rút tiền mặt VNĐ chi tiêu mà không cần bán tháo crypto. Hoặc gửi USDT nhận lãi suất tiền gửi lên đến 10.5%/năm qua VietQR/NAPAS 24/7.
                  </p>
                  <a
                    href="#lend"
                    className="btn btn-secondary btn-sm"
                    onClick={(e) => { e.preventDefault(); setActiveTab("lend"); }}
                  >
                    Khám Phá Lãi Suất
                  </a>
                </div>

                <div className="feature-card">
                  <div className="feature-icon-wrapper purple">
                    <FileText size={24} />
                  </div>
                  <h3 className="feature-title">Dịch Vụ Thuế Crypto Chuẩn NQ 05</h3>
                  <p className="feature-desc">
                    Tự động tính thuế Thu Nhập Cá Nhân (TNCN) phương pháp FIFO, xuất biểu mẫu Mẫu 02/KK-TNCN nộp chi cục thuế và mã hóa dữ liệu theo Nghị Định 13/2023/NĐ-CP.
                  </p>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={() => setCurrentPage("tax")}
                  >
                    Mở Bộ Tính Thuế
                  </button>
                </div>
              </div>
            </div>
          </section>

          {/* REGULATORY COMPLIANCE BANNER */}
          <section className="section">
            <div className="app-container">
              <div className="glass-panel" style={{ padding: "40px", borderLeft: "4px solid var(--accent-cyan)" }}>
                <div style={{ display: "flex", gap: "24px", alignItems: "flex-start", flexWrap: "wrap" }}>
                  <div style={{
                    width: "56px",
                    height: "56px",
                    borderRadius: "14px",
                    background: "rgba(0, 242, 254, 0.15)",
                    color: "var(--accent-cyan)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    flexShrink: 0
                  }}>
                    <Scale size={28} />
                  </div>
                  <div style={{ flex: 1, minWidth: "280px" }}>
                    <h3 style={{ fontSize: "1.4rem", fontWeight: "700", marginBottom: "8px" }}>
                      Định Hướng Tuân Thủ Nghị Quyết 05/2025/NQ-CP &amp; Nghị Định 13
                    </h3>
                    <p style={{ color: "var(--text-secondary)", lineHeight: "1.6", marginBottom: "16px" }}>
                      VietDeFi tiên phong áp dụng tiêu chuẩn an toàn cấp độ 4, tách biệt hoàn toàn tài sản người dùng, hỗ trợ cơ chế định danh KYC đa tầng và tương thích với sandbox sàn tài sản mã hóa CAEX / SCEX. Dữ liệu cá nhân được mã hóa AES-256 theo tiêu chuẩn Bảo vệ Dữ liệu Cá nhân (PDPD).
                    </p>
                    <div style={{ display: "flex", gap: "12px", flexWrap: "wrap" }}>
                      <span className="badge badge-emerald">Bảo Mật Cấp Độ 4</span>
                      <span className="badge badge-emerald">Tách Biệt Tài Sản Khách Hàng</span>
                      <span className="badge badge-gold">Định Danh KYC/AML 2022</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </section>
        </main>
      )}

      {/* TAX SERVICE PAGE */}
      {currentPage === "tax" && (
        <main className="section" style={{ minHeight: "80vh" }}>
          <div className="app-container">
            <div className="section-header" style={{ textAlign: "left", marginBottom: "32px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "16px" }}>
                <div>
                  <span className="section-tag">Kê Khai &amp; Báo Cáo Thuế</span>
                  <h1 className="section-title" style={{ fontSize: "2.4rem" }}>
                    Phần Mềm Tính Thuế Crypto Việt Nam
                  </h1>
                  <p className="section-subtitle" style={{ margin: 0 }}>
                    Tự động tính thuế TNCN theo Nghị quyết 05/2025/NQ-CP &amp; Hướng dẫn Tổng Cục Thuế.
                  </p>
                </div>
                <button
                  className="btn btn-primary"
                  onClick={() => {
                    alert("Đã xuất báo cáo thuế PDF Mẫu số 02/KK-TNCN mã hóa SHA-256 thành công!");
                  }}
                >
                  <Download size={16} /> Xuất Mẫu 02/KK-TNCN (PDF)
                </button>
              </div>
            </div>

            {/* Tax Overview Cards */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: "20px", marginBottom: "32px" }}>
              <div className="glass-panel" style={{ padding: "20px" }}>
                <span style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>Tổng Doanh Thu Chuyển Nhượng</span>
                <h3 style={{ fontSize: "1.6rem", fontWeight: "700", marginTop: "6px", fontFamily: "var(--font-mono)" }}>
                  {formatVND(taxReport.totalGrossProceedsVND)}
                </h3>
              </div>

              <div className="glass-panel" style={{ padding: "20px" }}>
                <span style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>Tổng Giá Vốn (Cost Basis)</span>
                <h3 style={{ fontSize: "1.6rem", fontWeight: "700", marginTop: "6px", fontFamily: "var(--font-mono)" }}>
                  {formatVND(taxReport.totalCostBasisVND)}
                </h3>
              </div>

              <div className="glass-panel" style={{ padding: "20px" }}>
                <span style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>Thu Nhập Chịu Thuế (Lãi Ròng)</span>
                <h3 style={{ fontSize: "1.6rem", fontWeight: "700", marginTop: "6px", color: "var(--accent-cyan)", fontFamily: "var(--font-mono)" }}>
                  {formatVND(taxReport.netTaxableGainVND)}
                </h3>
              </div>

              <div className="glass-panel" style={{ padding: "20px", border: "1px solid rgba(245, 158, 11, 0.4)" }}>
                <span style={{ fontSize: "0.85rem", color: "var(--accent-gold)", fontWeight: "600" }}>Ước Tính Thuế TNCN Phải Nộp (5%)</span>
                <h3 style={{ fontSize: "1.6rem", fontWeight: "700", marginTop: "6px", color: "var(--accent-gold)", fontFamily: "var(--font-mono)" }}>
                  {formatVND(taxReport.estimatedTaxPayableVND)}
                </h3>
              </div>
            </div>

            {/* Cost Basis Method Selector */}
            <div className="glass-panel" style={{ padding: "24px", marginBottom: "32px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px", flexWrap: "wrap", gap: "12px" }}>
                <h3 style={{ fontSize: "1.15rem", fontWeight: "700" }}>Phương Pháp Xác Định Giá Vốn</h3>
                <div style={{ display: "flex", gap: "8px" }}>
                  <button
                    className={`btn btn-sm ${taxMethod === "fifo" ? "btn-primary" : "btn-secondary"}`}
                    onClick={() => setTaxMethod("fifo")}
                  >
                    FIFO (Nhập trước Xuất trước)
                  </button>
                  <button
                    className={`btn btn-sm ${taxMethod === "weighted_average" ? "btn-primary" : "btn-secondary"}`}
                    onClick={() => setTaxMethod("weighted_average")}
                  >
                    Bình Quân Gia Quyền
                  </button>
                </div>
              </div>
              <p style={{ fontSize: "0.9rem", color: "var(--text-secondary)", lineHeight: "1.6" }}>
                * Phương pháp <strong>FIFO</strong> được khuyến nghị hàng đầu bởi cơ quan thuế Việt Nam đối với các danh mục tài sản số, đảm bảo việc xác định chi phí lịch sử minh bạch và đối chiếu chuỗi khối dễ dàng.
              </p>
            </div>

            {/* Transaction Ledger Table */}
            <div className="glass-panel" style={{ padding: "24px", overflowX: "auto" }}>
              <h3 style={{ fontSize: "1.15rem", fontWeight: "700", marginBottom: "16px" }}>
                Nhật Ký Giao Dịch Tính Thuế ({SAMPLE_TRANSACTIONS.length} giao dịch)
              </h3>
              <table className="custom-table">
                <thead>
                  <tr>
                    <th>Thời Gian</th>
                    <th>Loại Giao Dịch</th>
                    <th>Tài Sản Bán / Trả</th>
                    <th>Tài Sản Mua / Nhận</th>
                    <th>Giá Trị Chuyển Nhượng</th>
                    <th>Trạng Thái</th>
                  </tr>
                </thead>
                <tbody>
                  {SAMPLE_TRANSACTIONS.map((tx) => (
                    <tr key={tx.id}>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.85rem" }}>
                        {new Date(tx.timestamp).toLocaleDateString("vi-VN")}
                      </td>
                      <td>
                        <span className="badge badge-emerald">{tx.type}</span>
                      </td>
                      <td>{tx.amountIn} {tx.assetIn}</td>
                      <td>{tx.amountOut} {tx.assetOut}</td>
                      <td style={{ fontFamily: "var(--font-mono)", fontWeight: "600", color: "#fff" }}>
                        {formatVND(tx.amountOut * tx.priceOutVND)}
                      </td>
                      <td>
                        <span style={{ color: "var(--accent-emerald)", display: "flex", alignItems: "center", gap: "4px", fontSize: "0.85rem" }}>
                          <CheckCircle2 size={14} /> Đã tính thuế
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </main>
      )}

      {/* CUSTOM 404 PAGE */}
      {currentPage === "404" && (
        <main className="section" style={{ minHeight: "75vh", display: "flex", alignItems: "center" }}>
          <div className="app-container" style={{ textAlign: "center", maxWidth: "640px" }}>
            <div style={{
              fontSize: "6rem",
              fontWeight: "900",
              fontFamily: "var(--font-mono)",
              background: "linear-gradient(135deg, #00f2fe, #8b5cf6)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
              lineHeight: 1
            }}>
              404
            </div>
            <h2 style={{ fontSize: "2rem", fontWeight: "800", marginTop: "16px", marginBottom: "12px" }}>
              Trang Không Tồn Tại
            </h2>
            <p style={{ color: "var(--text-secondary)", marginBottom: "32px", lineHeight: "1.6" }}>
              Đường dẫn bạn yêu cầu không nằm trong hệ thống mạng của VietDeFi hoặc đã được di chuyển sang smart contract mới.
            </p>

            <div style={{ display: "flex", gap: "12px", justifyContent: "center", flexWrap: "wrap", marginBottom: "36px" }}>
              <button className="btn btn-primary" onClick={() => setCurrentPage("home")}>
                Về Trang Chủ
              </button>
              <button className="btn btn-secondary" onClick={() => { setCurrentPage("home"); setActiveTab("swap"); }}>
                Sàn Đổi 1:1
              </button>
              <button className="btn btn-secondary" onClick={() => setCurrentPage("tax")}>
                Tính Thuế Crypto
              </button>
            </div>

            <div className="glass-panel" style={{ padding: "16px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "10px", color: "var(--text-muted)" }}>
                <Search size={18} />
                <input
                  type="text"
                  placeholder="Tìm kiếm tài liệu, hợp đồng thông minh..."
                  style={{
                    background: "transparent",
                    border: "none",
                    outline: "none",
                    color: "#fff",
                    width: "100%",
                    fontFamily: "inherit"
                  }}
                />
              </div>
            </div>
          </div>
        </main>
      )}

      {/* THANK YOU / TRANSACTION SUCCESS PAGE */}
      {currentPage === "thankyou" && (
        <main className="section" style={{ minHeight: "75vh", display: "flex", alignItems: "center" }}>
          <div className="app-container" style={{ maxWidth: "600px" }}>
            <div className="glass-panel" style={{ padding: "40px", textAlign: "center", borderTop: "4px solid var(--accent-emerald)" }}>
              <div style={{
                width: "72px",
                height: "72px",
                borderRadius: "50%",
                background: "rgba(16, 185, 129, 0.15)",
                color: "var(--accent-emerald)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                margin: "0 auto 24px"
              }}>
                <CheckCircle2 size={40} />
              </div>

              <h2 style={{ fontSize: "2rem", fontWeight: "800", marginBottom: "8px" }}>
                Giao Dịch Thành Công!
              </h2>
              <p style={{ color: "var(--text-secondary)", marginBottom: "24px" }}>
                Cảm ơn bạn đã sử dụng nền tảng tài chính số VietDeFi. Lệnh của bạn đã được ghi nhận trên sổ cái chuỗi khối và lưu vết kiểm toán.
              </p>

              <div style={{
                background: "var(--bg-input)",
                padding: "16px",
                borderRadius: "12px",
                textAlign: "left",
                marginBottom: "28px",
                fontSize: "0.88rem"
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "10px" }}>
                  <span style={{ color: "var(--text-muted)" }}>Mã Băm Giao Dịch (TxHash):</span>
                  <span style={{ fontFamily: "var(--font-mono)", color: "var(--accent-cyan)" }}>
                    {lastTxHash ? `${lastTxHash.slice(0, 10)}...${lastTxHash.slice(-8)}` : "0x9f8c...33a2"}
                  </span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "10px" }}>
                  <span style={{ color: "var(--text-muted)" }}>Khối Xác Thực:</span>
                  <span style={{ color: "var(--accent-emerald)" }}>12 Blocks (Finalized)</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-muted)" }}>Lưu Vết Thuế:</span>
                  <span>Tự động cập nhật Mẫu 02</span>
                </div>
              </div>

              <div style={{ display: "flex", gap: "12px", justifyContent: "center", flexWrap: "wrap" }}>
                <button className="btn btn-primary" onClick={() => setCurrentPage("home")}>
                  Thực Hiện Giao Dịch Mới
                </button>
                <button
                  className="btn btn-secondary"
                  onClick={() => alert("Đã tải biên lai giao dịch điện tử kèm mã QR VietQR!")}
                >
                  <Download size={16} /> Tải Biên Lai
                </button>
              </div>
            </div>
          </div>
        </main>
      )}

      {/* CAP CAPTCHA & CONFIRMATION MODAL */}
      {isTxModalOpen && (
        <div style={{
          position: "fixed",
          inset: 0,
          background: "rgba(5, 8, 17, 0.85)",
          backdropFilter: "blur(12px)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 999,
          padding: "20px"
        }}>
          <div className="glass-panel" style={{ maxWidth: "460px", width: "100%", padding: "32px", position: "relative" }}>
            <h3 style={{ fontSize: "1.35rem", fontWeight: "700", marginBottom: "8px" }}>
              Xác Thực Giao Dịch An Toàn
            </h3>
            <p style={{ color: "var(--text-secondary)", fontSize: "0.9rem", marginBottom: "20px" }}>
              Hệ thống tự lưu trữ Cap CAPTCHA (Bảo mật quyền riêng tư, không thu thập dữ liệu cá nhân theo Nghị định 13).
            </p>

            {/* Cap Self-Hosted Captcha Simulation / Widget Box */}
            <div className="cap-box">
              <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                <button
                  onClick={handleSimulateCapSolve}
                  style={{
                    width: "28px",
                    height: "28px",
                    borderRadius: "6px",
                    border: capSolved ? "2px solid #10b981" : "2px solid rgba(255, 255, 255, 0.3)",
                    background: capSolved ? "#10b981" : "transparent",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    cursor: "pointer",
                    color: "#050811"
                  }}
                >
                  {capSolved && <CheckCircle2 size={18} color="#fff" />}
                </button>
                <span style={{ fontSize: "0.92rem", fontWeight: "500" }}>
                  {capSolved ? "Xác minh con người thành công" : "Nhấp để xác thực Proof-of-Work"}
                </span>
              </div>
              <div style={{ textAlign: "right" }}>
                <span style={{ fontSize: "0.75rem", color: "var(--accent-cyan)", fontWeight: "700" }}>Cap Core</span>
                <div style={{ fontSize: "0.68rem", color: "var(--text-muted)" }}>Mã nguồn mở</div>
              </div>
            </div>

            <div style={{ display: "flex", gap: "12px", marginTop: "24px" }}>
              <button
                className="btn btn-secondary"
                style={{ flex: 1 }}
                onClick={() => setIsTxModalOpen(false)}
              >
                Hủy Bỏ
              </button>
              <button
                className="btn btn-primary"
                style={{ flex: 1 }}
                disabled={!capSolved || txSubmitting}
                onClick={handleConfirmTransaction}
              >
                {txSubmitting ? "Đang Xử Lý..." : "Xác Nhận Ký Lệnh"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* FOOTER & INTERNAL LINKING GRAPH */}
      <footer className="footer">
        <div className="app-container">
          <div className="footer-grid">
            <div>
              <div className="brand-logo" style={{ marginBottom: "16px" }}>
                <div style={{
                  width: "32px",
                  height: "32px",
                  borderRadius: "8px",
                  background: "linear-gradient(135deg, #00f2fe, #4facfe)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "#050811",
                  fontWeight: "bold"
                }}>
                  ₫
                </div>
                <span>VND<span className="brand-gradient">-X</span></span>
              </div>
              <p style={{ color: "var(--text-secondary)", fontSize: "0.9rem", lineHeight: "1.6", maxWidth: "340px", marginBottom: "16px" }}>
                Hạ tầng tài chính phi tập trung thế hệ mới dành riêng cho thị trường Việt Nam. Hoán đổi stablecoin 1:1, đúc tiền đồng VNDL bảo chứng vàng và giải pháp thuế điện tử.
              </p>
              <div style={{ display: "flex", alignItems: "center", gap: "8px", color: "var(--accent-emerald)", fontSize: "0.85rem" }}>
                <Lock size={14} />
                <span>Mã hóa AES-256 &amp; Nhật Ký Audit Bất Biến</span>
              </div>
            </div>

            <div className="footer-column">
              <h4>Sản Phẩm Cốt Lõi</h4>
              <ul className="footer-links">
                <li>
                  <a href="#swap" className="footer-link" onClick={() => { setCurrentPage("home"); setActiveTab("swap"); }}>
                    Hoán Đổi 1:1 PSM
                  </a>
                </li>
                <li>
                  <a href="#borrow" className="footer-link" onClick={() => { setCurrentPage("home"); setActiveTab("borrow"); }}>
                    Đúc VNDL (USDT/XAUt)
                  </a>
                </li>
                <li>
                  <a href="#lend" className="footer-link" onClick={() => { setCurrentPage("home"); setActiveTab("lend"); }}>
                    Thị Trường Vay Tiền Đồng
                  </a>
                </li>
                <li>
                  <a href="#tax" className="footer-link" onClick={() => setCurrentPage("tax")}>
                    Tính Thuế Cá Nhân Crypto
                  </a>
                </li>
              </ul>
            </div>

            <div className="footer-column">
              <h4>Pháp Lý &amp; Tiêu Chuẩn</h4>
              <ul className="footer-links">
                <li>
                  <a href="https://www.caex.com.vn/" target="_blank" rel="noopener noreferrer" className="footer-link">
                    Sandbox Nghị Quyết 05/2025/NQ-CP
                  </a>
                </li>
                <li>
                  <a href="#compliance" className="footer-link" onClick={() => setCurrentPage("home")}>
                    Nghị Định 13/2023/NĐ-CP (PDPD)
                  </a>
                </li>
                <li>
                  <a href="#kyc" className="footer-link">
                    Tiêu Chuẩn Phòng Chống Rửa Tiền AML
                  </a>
                </li>
                <li>
                  <a href="/sitemap.xml" className="footer-link">
                    Sitemap Hệ Thống (XML)
                  </a>
                </li>
              </ul>
            </div>

            <div className="footer-column">
              <h4>Hạ Tầng &amp; Đối Tác</h4>
              <ul className="footer-links">
                <li>
                  <a href="https://dash.cloudflare.com/" target="_blank" rel="noopener noreferrer" className="footer-link">
                    Cloudflare Pages &amp; Edge Workers
                  </a>
                </li>
                <li>
                  <a href="https://supabase.com/" target="_blank" rel="noopener noreferrer" className="footer-link">
                    Supabase PostgreSQL &amp; RLS
                  </a>
                </li>
                <li>
                  <a href="https://trycap.dev/" target="_blank" rel="noopener noreferrer" className="footer-link">
                    Cap Self-Hosted CAPTCHA
                  </a>
                </li>
                <li>
                  <a href="https://scex.com.vn/" target="_blank" rel="noopener noreferrer" className="footer-link">
                    Cổng Thanh Khoản SCEX / CAEX
                  </a>
                </li>
              </ul>
            </div>
          </div>

          <div className="footer-bottom">
            <div>
              &copy; 2026 VietDeFi Protocol (VND-X). Bảo lưu mọi quyền theo quy định pháp luật Việt Nam.
            </div>
            <div style={{ display: "flex", gap: "24px" }}>
              <a href="/robots.txt" className="footer-link">Robots.txt</a>
              <a href="/sitemap.xml" className="footer-link">Sitemap</a>
              <button
                onClick={() => setCurrentPage("404")}
                style={{ background: "transparent", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: "0.85rem" }}
              >
                Trang Lỗi 404
              </button>
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
}
