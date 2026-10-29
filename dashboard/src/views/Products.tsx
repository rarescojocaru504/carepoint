import { type FormEvent, useCallback, useEffect, useState } from "react";

import { type Product, type ProductCategory, api, errorMessage } from "../api";
import { money } from "../format";

const CATEGORIES: ProductCategory[] = [
  "hygiene", "menstrual_care", "sexual_health", "first_aid", "pharmacy", "baby_care", "essentials",
];

export function Products() {
  const [products, setProducts] = useState<Product[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);

  const load = useCallback(() => {
    api.products().then(setProducts, (e: unknown) => setError(errorMessage(e)));
  }, []);
  useEffect(load, [load]);

  const act = (action: Promise<unknown>): void => {
    action.then(
      () => {
        setError(null);
        load();
      },
      (e: unknown) => setError(errorMessage(e)),
    );
  };

  return (
    <div className="page">
      <h1>Products</h1>
      {error && <p className="error">{error}</p>}
      <NewProduct onCreated={load} onError={setError} />
      <section className="card">
        <table className="table" data-testid="products">
          <thead>
            <tr>
              <th>SKU</th>
              <th>Name</th>
              <th>Category</th>
              <th className="num">Price</th>
              <th>State</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(products ?? []).map((p) =>
              editing === p.id ? (
                <EditRow key={p.id} product={p} done={() => { setEditing(null); load(); }} onError={setError} />
              ) : (
                <tr key={p.id} className={p.is_active ? undefined : "inactive"}>
                  <td className="mono">{p.sku}</td>
                  <td>{p.name}</td>
                  <td className="muted">{p.category.replace("_", " ")}</td>
                  <td className="num">{money(p.price_cents, p.currency)}</td>
                  <td>{p.is_active ? <span className="badge good">on sale</span> : <span className="badge">inactive</span>}</td>
                  <td className="row-actions">
                    <button type="button" className="button small" onClick={() => setEditing(p.id)}>Edit</button>
                    <button type="button" className="button small"
                      onClick={() => act(api.updateProduct(p.id, { is_active: !p.is_active }))}>
                      {p.is_active ? "Deactivate" : "Activate"}
                    </button>
                    <button type="button" className="button small danger"
                      onClick={() => window.confirm(`Delete ${p.sku}?`) && act(api.deleteProduct(p.id))}>
                      Delete
                    </button>
                  </td>
                </tr>
              ),
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}

function EditRow({ product, done, onError }: { product: Product; done: () => void; onError: (m: string) => void }) {
  const [name, setName] = useState(product.name);
  const [price, setPrice] = useState((product.price_cents / 100).toFixed(2));
  const save = (): void => {
    api.updateProduct(product.id, { name: name.trim(), price_cents: Math.round(Number(price) * 100) })
      .then(done, (e: unknown) => onError(errorMessage(e)));
  };
  return (
    <tr className="editing">
      <td className="mono">{product.sku}</td>
      <td><input value={name} onChange={(e) => setName(e.target.value)} aria-label="Name" /></td>
      <td className="muted">{product.category.replace("_", " ")}</td>
      <td className="num"><input value={price} onChange={(e) => setPrice(e.target.value)} aria-label="Price" inputMode="decimal" size={7} /></td>
      <td />
      <td className="row-actions">
        <button type="button" className="button small primary" onClick={save}>Save</button>
        <button type="button" className="button small" onClick={done}>Cancel</button>
      </td>
    </tr>
  );
}

function NewProduct({ onCreated, onError }: { onCreated: () => void; onError: (m: string | null) => void }) {
  const [sku, setSku] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState<ProductCategory>("essentials");
  const [price, setPrice] = useState("");

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    api.createProduct({
      sku: sku.trim().toUpperCase(), name: name.trim(), category, description: null, image_url: null,
      price_cents: Math.round(Number(price) * 100), currency: "RON",
    }).then(() => {
      setSku(""); setName(""); setPrice(""); onError(null); onCreated();
    }, (e: unknown) => onError(errorMessage(e)));
  };

  return (
    <form className="card new-product" onSubmit={submit} aria-label="New product">
      <h2>New product</h2>
      <input value={sku} onChange={(e) => setSku(e.target.value)} placeholder="SKU, e.g. LIP-BALM" required aria-label="SKU" />
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" required aria-label="Product name" />
      <select value={category} onChange={(e) => setCategory(e.target.value as ProductCategory)} aria-label="Category">
        {CATEGORIES.map((c) => <option key={c} value={c}>{c.replace("_", " ")}</option>)}
      </select>
      <input value={price} onChange={(e) => setPrice(e.target.value)} placeholder="Price (RON)" required inputMode="decimal" aria-label="New price" />
      <button type="submit" className="button primary">Add</button>
    </form>
  );
}
