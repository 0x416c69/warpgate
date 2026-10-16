use anyhow::{Context, Result};
use tokio::time::timeout;
use warpgate_common::GlobalParams;

use crate::config::load_config;

pub async fn command(params: &GlobalParams) -> Result<()> {
    let config = load_config(params, true)?;

    let url = format!(
        "{}://{}/@warpgate/api/info",
        if config.store.http.tls {
            "https"
        } else {
            "http"
        },
        config.store.http.listen.address()
    );

    let client = reqwest::Client::builder()
        .danger_accept_invalid_certs(true)
        .use_rustls_tls()
        .build()?;

    let response = timeout(std::time::Duration::from_secs(5), client.get(&url).send())
        .await
        .context("Timeout")?
        .context("Failed to send request")?;

    response.error_for_status()?;

    Ok(())
}
