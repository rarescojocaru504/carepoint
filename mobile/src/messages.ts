// What to tell the customer for each business error code the API returns.

import { ApiError, errorMessage } from "./services/api";

const BY_CODE: Record<string, string> = {
  insufficient_stock: "Sorry, someone just reserved the last one.",
  too_many_active:
    "You already have as many active reservations as allowed. Pick one up or cancel one first.",
  not_sellable: "This item can't be reserved right now.",
  payment_declined: "Your card was declined. Try another card.",
  payment_unavailable: "Payments are briefly unavailable. Please try again.",
  not_payable: "This reservation can't be paid any more.",
  not_cancellable: "This reservation can't be cancelled any more.",
  pickup_in_progress: "The machine is dispensing your item right now.",
};

export function friendlyError(error: unknown): string {
  if (error instanceof ApiError && error.code !== null) {
    return BY_CODE[error.code] ?? errorMessage(error);
  }
  return errorMessage(error);
}
