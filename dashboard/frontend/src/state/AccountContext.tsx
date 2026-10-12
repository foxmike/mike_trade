/** Selected-account context. The choice persists in localStorage and is
 * validated against the live account registry; account-scoped queries key on
 * this value so data from different accounts is never mixed. */

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useAccounts } from "./queries";
import type { AccountSummary } from "../api/client";

const STORAGE_KEY = "mike-trade-console-account";

interface AccountContextValue {
  accounts: AccountSummary[];
  accountsLoading: boolean;
  accountsError: string | null;
  selectedAccountId: string | null;
  selectAccount: (accountId: string) => void;
}

const AccountContext = createContext<AccountContextValue>({
  accounts: [],
  accountsLoading: true,
  accountsError: null,
  selectedAccountId: null,
  selectAccount: () => undefined,
});

function modeGroupOrder(mode: string): number {
  const normalized = mode.toUpperCase();
  if (normalized === "LIVE") return 0;
  if (normalized === "PAPER") return 1;
  if (normalized === "SHADOW") return 2;
  return 3;
}

function readStoredAccount(valid: Set<string>): string | null {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored !== null && valid.has(stored) ? stored : null;
  } catch {
    return null;
  }
}

export function AccountProvider({ children }: { children: ReactNode }) {
  const accountsQuery = useAccounts();
  const accounts = useMemo(() => accountsQuery.data?.accounts ?? [], [accountsQuery.data]);
  const validIds = useMemo(() => new Set(accounts.map((account) => account.account_id)), [accounts]);
  const [selected, setSelected] = useState<string | null>(() => null);

  useEffect(() => {
    if (validIds.size === 0) return;
    setSelected((current) => {
      if (current !== null && validIds.has(current)) return current;
      const stored = readStoredAccount(validIds);
      if (stored !== null) return stored;
      const ordered = [...accounts].sort(
        (a, b) => modeGroupOrder(a.mode) - modeGroupOrder(b.mode),
      );
      return ordered[0]?.account_id ?? null;
    });
  }, [validIds, accounts]);

  const selectAccount = (accountId: string) => {
    if (!validIds.has(accountId)) return;
    try {
      window.localStorage.setItem(STORAGE_KEY, accountId);
    } catch {
      // Storage unavailable; selection stays in memory only.
    }
    setSelected(accountId);
  };

  const value: AccountContextValue = {
    accounts,
    accountsLoading: accountsQuery.isPending,
    accountsError:
      accountsQuery.isError && accounts.length === 0
        ? accountsQuery.error instanceof Error
          ? accountsQuery.error.message
          : "Account list load failed"
        : null,
    selectedAccountId: selected !== null && validIds.has(selected) ? selected : null,
    selectAccount,
  };

  return <AccountContext.Provider value={value}>{children}</AccountContext.Provider>;
}

export function useSelectedAccount(): AccountContextValue {
  return useContext(AccountContext);
}
