import IndexChart from "@/components/IndexChart";
import LeadTimeChart from "@/components/LeadTimeChart";
import Chat from "@/components/Chat";
import {
  direction,
  fmt,
  getDaily,
  getLeadTime,
  getRoutes,
  signed,
  supabase,
} from "@/lib/api";

export const revalidate = 900; // the index updates once a day; 15 min is ample

export default async function Page() {
  const [daily, routes, leadTime] = await Promise.all([
    getDaily(),
    getRoutes(),
    getLeadTime(),
  ]);

  const latest = daily.at(-1);
  const asOf = latest
    ? new Date(latest.quote_date).toLocaleDateString("en-IN", {
        day: "numeric",
        month: "long",
        year: "numeric",
      })
    : null;

  return (
    <main className="shell">
      <header className="masthead">
        <span className="wordmark">APIx</span>
        <span className="meta">
          Airfare Price Index · base 2024 = 100
          {asOf ? ` · as at ${asOf}` : ""}
        </span>
      </header>

      {!supabase && (
        <section>
          <h1>Connect the database to see the index</h1>
          <p className="empty">
            Set <code>NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
            <code>NEXT_PUBLIC_SUPABASE_ANON_KEY</code> in your Vercel project
            settings, then redeploy. Use the anon key, not the service role key.
          </p>
        </section>
      )}

      {supabase && !latest && (
        <section>
          <h1>No index has been published yet</h1>
          <p className="empty">
            The collector writes to Supabase once a day and the index is rebuilt
            straight after. Run <code>python -m scripts.collect</code> followed
            by <code>python -m scripts.rebuild</code>, or trigger the{" "}
            <code>Collect airfares</code> workflow.
          </p>
        </section>
      )}

      {latest && (
        <>
          <section>
            <div className="headline-value">{fmt(latest.apix)}</div>
            <div className={`delta ${direction(latest.dod_pct)}`}>
              {signed(latest.dod_pct)} on the previous day
            </div>
            <div className="subline">
              {signed(latest.mom_pct)} over 30 days ·{" "}
              {routes.length} routes ·{" "}
              {fmt(latest.pct_imputed, 1)}% of cells imputed
            </div>
          </section>

          <section>
            <h2>Daily index</h2>
            <IndexChart data={daily} />
            <p className="note">
              Each point is a weighted mean of route-level price relatives
              against the base period. Elementary aggregates use the Jevons
              geometric mean across carriers, matching the formula NSO uses for
              CPI 2024.
            </p>
          </section>

          <div className="split">
            <section>
              <h2>Routes</h2>
              <table>
                <thead>
                  <tr>
                    <th>Route</th>
                    <th className="num">Index</th>
                    <th className="num">Imputed</th>
                  </tr>
                </thead>
                <tbody>
                  {routes.slice(0, 12).map((r) => (
                    <tr key={r.route}>
                      <td className="route">{r.route}</td>
                      <td className="num">{fmt(r.index_value)}</td>
                      <td
                        className="num"
                        style={{
                          color:
                            (r.pct_imputed ?? 0) > 25
                              ? "var(--provisional)"
                              : "var(--ink-soft)",
                        }}
                      >
                        {fmt(r.pct_imputed, 0)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="note">
                Route weights come from DGCA domestic city-pair passenger
                traffic, twelve-month trailing sum. Values above 25% imputed are
                marked; treat them as provisional.
              </p>
            </section>

            <section>
              <h2>Cost of booking late</h2>
              <LeadTimeChart data={leadTime} />
              <p className="note assumption">
                Median fare at each advance-purchase window. How much each
                window contributes to the headline index rests on an assumed
                booking profile — India publishes no lead-time distribution, so
                this weighting is an assumption, not a measurement.
              </p>
            </section>
          </div>

          <section>
            <h2>Ask about the index</h2>
            <Chat />
          </section>
        </>
      )}

      <section>
        <p className="note">
          APIx is a research prototype. It is not an official statistic and not
          a fare quote. Index values describe a fixed basket of directional
          routes and booking horizons, so they will not match what any single
          traveller pays.
        </p>
      </section>
    </main>
  );
}
