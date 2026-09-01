"""Manual entry point for phase-4 training runs.

Training is a job a human starts and reads the output of, not something the scheduler does
on its own - a model that promotes itself eventually promotes a regression unnoticed.

    docker compose --profile ml run --rm trainer python -m app.ml.cli train
    docker compose --profile ml run --rm trainer python -m app.ml.cli models

`drift` is the exception - it reads the prediction log and needs no ml extra, so it runs in
the API image too:

    docker compose exec api python -m app.ml.cli drift
"""

import argparse
import asyncio

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine
from app.ml.evaluate import format_report
from app.ml.pipeline import DEFAULT_ROUNDS, train
from app.ml.registry import list_models
from app.workers.drift import check_calibration_drift


async def cmd_train(rounds: int, notes: str, weighted: bool) -> None:
    result = await train(rounds=rounds, notes=notes, weighted=weighted)

    print(f"version:  {result.version}")
    print(f"artifact: {result.booster_path}")
    print(
        f"train:    {result.card.train_matches} matches / {result.card.train_rows} rows "
        f"({result.card.train_window[0]} .. {result.card.train_window[-1]})"
    )
    print(
        f"holdout:  {result.card.holdout_matches} matches / {result.card.holdout_rows} rows "
        f"({result.card.holdout_window[0]} .. {result.card.holdout_window[-1]})"
    )
    print()
    print(format_report(result.evaluation))
    print()

    if result.card.passes_gate:
        print("This model may be served. To activate it, set in .env and restart the API:")
        print(f"    ACTIVE_MODEL_VERSION={result.version}")
    else:
        print("Not activated: it did not beat every baseline in every minute bucket.")
        print("The artifact and its card are on disk for inspection.")


async def cmd_models() -> None:
    cards = list_models(get_settings().model_dir)
    if not cards:
        print("no trained models yet")
        return

    print(f"{'version':<24} {'gate':<8} {'log loss':>9} {'brier':>8} {'ECE':>7}  holdout")
    for card in cards:
        gate = "pass" if card.passes_gate else f"FAIL({len(card.gate_failures)})"
        print(
            f"{card.version:<24} {gate:<8} {card.holdout_log_loss:>9.4f} "
            f"{card.holdout_brier:>8.4f} {card.holdout_ece:>7.4f}  "
            f"{card.holdout_matches} matches"
        )


async def cmd_drift() -> None:
    """Run the calibration check by hand.

    The same code the daily cron runs, so what is printed here is exactly what would have
    been logged - an alarm nobody can reproduce on demand is an alarm nobody trusts.
    """
    alerting = await check_calibration_drift({})
    print(f"versions drifting: {alerting}")
    print("Per-version verdicts are in the log lines above.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.ml.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    trainer = sub.add_parser("train", help="train, calibrate, score against the baselines")
    trainer.add_argument(
        "--rounds",
        type=int,
        default=DEFAULT_ROUNDS,
        help=f"boosting rounds before early stopping (default: {DEFAULT_ROUNDS})",
    )
    trainer.add_argument(
        "--notes", default="", help="free text stored on the model card, e.g. what changed"
    )
    trainer.add_argument(
        "--no-weights",
        dest="weighted",
        action="store_false",
        help="train without section 5.4 tier weights (they are on by default; see TIER_WEIGHTS)",
    )

    sub.add_parser("models", help="list trained models and whether they passed the gate")

    sub.add_parser(
        "drift", help="check served predictions for a rise in calibration error (phase 7)"
    )

    args = parser.parse_args()
    configure_logging(get_settings().log_level)

    async def run() -> None:
        try:
            if args.command == "train":
                await cmd_train(args.rounds, args.notes, args.weighted)
            elif args.command == "drift":
                await cmd_drift()
            else:
                await cmd_models()
        finally:
            await dispose_engine()

    asyncio.run(run())


if __name__ == "__main__":
    main()
