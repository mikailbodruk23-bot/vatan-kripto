
// VATAN 2 - Binance Futures Canli Veri
// Sadece herkese acik piyasa verilerini okur.
// Emir acmaz. API anahtari gerekmez.

const VATAN_SYMBOLS = ["btcusdt", "ethusdt", "solusdt"];

let vatanSocket = null;

function vatanConnect() {
  const streams = VATAN_SYMBOLS
    .map(symbol => `${symbol}@markPrice@1s`)
    .join("/");

  const url =
    `wss://fstream.binance.com/stream?streams=${streams}`;

  vatanSocket = new WebSocket(url);

  vatanSocket.onopen = () => {
    console.log("VATAN 2: Binance baglandi");
  };

  vatanSocket.onmessage = event => {
    try {
      const message = JSON.parse(event.data);
      const data = message.data;

      if (!data || !data.s || !data.p) return;

      const price = Number(data.p);

      if (!Number.isFinite(price)) return;

      window.dispatchEvent(
        new CustomEvent("vatan-price", {
          detail: {
            symbol: data.s,
            price,
            timestamp: data.E
          }
        })
      );

      console.log(data.s, price);
    } catch (error) {
      console.error("Veri hatasi:", error);
    }
  };

  vatanSocket.onerror = () => {
    console.warn("Binance baglanti hatasi");
  };

  vatanSocket.onclose = () => {
    console.warn("Binance baglantisi kapandi");
  };
}

vatanConnect();
