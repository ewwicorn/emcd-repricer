# EMCD P2P Repricer

Automated price adjustment tool for EMCD P2P offers.
This script monitors the market and updates your offer prices to stay competitive.


## Getting started

1. Install the dependencies by running (`setup.bat`) file.

2. Run the script using (`run.bat`) file.

3. Log in your account on the first run (session will remain for ~7 days)
## Configuration Overview

The configuration file (`config.yaml`) controls how the repricer behaves.

### Global Settings

```yaml
interval_min: 15
interval_max: 25
price_step: 0.01
dry_run: false
```

* **interval_min / interval_max**
  Time range (in seconds) between each pricing cycle.
  A random value is selected between these two to avoid predictable behavior.

* **price_step**
  Default amount added on top of the best market price.

* **dry_run**

  * `true` → Logs actions only (safe testing mode)
  * `false` → Actually updates prices

---

## Accounts

You can configure multiple accounts:

```yaml
accounts:
  - name: "acc1"
    email: "your_email"
    password: "your_password"
```

### Proxy Support (Optional)

```yaml
proxies:
  - "http://ip:port"
  - "http://user:pass@ip:port"
  - "socks5://ip:port"
```

* Proxies are used to distribute requests and reduce the chance of bans.
* If multiple proxies are provided, they can be rotated.

---

## Offers

Each account can manage multiple offers:

```yaml
offers:
  - offer_id: "uuid"
    crypto: "usdt"
    fiat: "rub"
    direction: "buy"
```

### Fields

* **offer_id**
  Unique identifier of your P2P offer.

* **crypto**
  Cryptocurrency type (`usdt`, `btc`, `eth`, etc.)

* **fiat**
  Fiat currency (`usd`, `rub`, etc.)

* **direction**

  * `buy` → You are buying crypto
  * `sell` → You are selling crypto

---

## Price Adjustment

```yaml
price_step: 0.01
```

* Overrides the global `price_step` for this specific offer.
* Your price will be set slightly above the top competitor.

---

## Ignored Users

```yaml
ignored_users:
  - "username1"
  - "username2"
```

* If the top offer belongs to one of these users, it will be skipped.
* The repricer will instead compare against the next available offer.

---

## How It Works

1. Logs into each account
2. Fetches current P2P market data
3. Finds the top competing offer
4. Skips ignored users if necessary
5. Adjusts your price using `price_step`
6. Repeats after a random delay

---

## Notes

* Use proxies if running multiple accounts.
* Avoid too aggressive intervals to reduce ban risk.
* Always test in `dry_run` mode before going live.

---

## Example of (`config.yaml`) 

```yaml
interval_min: 15
interval_max: 25
price_step: 0.01
dry_run: false

accounts:
  - name: "acc1"
    email: "example@mail.com"
    password: "password"

    offers:
      - offer_id: "example-id"
        crypto: "usdt"
        fiat: "rub"
        direction: "buy"
        price_step: 0.01
        ignored_users:
          - "example_user"
```

---
