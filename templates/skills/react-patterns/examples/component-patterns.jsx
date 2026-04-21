// React Component Pattern Examples

// ============================================
// 1. COMPOSITION PATTERN
// ============================================

// Good: Composable components
function Card({ children }) {
  return <div className="card">{children}</div>;
}

function CardHeader({ children }) {
  return <div className="card-header">{children}</div>;
}

function CardBody({ children }) {
  return <div className="card-body">{children}</div>;
}

// Usage
function UserProfile() {
  return (
    <Card>
      <CardHeader>
        <h2>John Doe</h2>
      </CardHeader>
      <CardBody>
        <p>Software Engineer</p>
      </CardBody>
    </Card>
  );
}

// ============================================
// 2. CUSTOM HOOKS PATTERN
// ============================================

// Good: Reusable logic
function useAuth() {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Fetch user from API
    fetchUser().then(user => {
      setUser(user);
      setLoading(false);
    });
  }, []);

  const login = async (credentials) => {
    const user = await api.login(credentials);
    setUser(user);
  };

  const logout = () => {
    api.logout();
    setUser(null);
  };

  return { user, loading, login, logout };
}

// Usage in component
function Profile() {
  const { user, loading } = useAuth();

  if (loading) return <Spinner />;
  if (!user) return <Login />;

  return <div>Welcome {user.name}</div>;
}

// ============================================
// 3. RENDER PROPS PATTERN
// ============================================

function DataFetcher({ url, render }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(url)
      .then(r => r.json())
      .then(data => {
        setData(data);
        setLoading(false);
      });
  }, [url]);

  return render({ data, loading });
}

// Usage
function UserList() {
  return (
    <DataFetcher
      url="/api/users"
      render={({ data, loading }) =>
        loading ? <Spinner /> : <UserTable users={data} />
      }
    />
  );
}

// ============================================
// 4. COMPOUND COMPONENTS PATTERN
// ============================================

const TabsContext = React.createContext();

function Tabs({ children, defaultTab }) {
  const [activeTab, setActiveTab] = useState(defaultTab);

  return (
    <TabsContext.Provider value={{ activeTab, setActiveTab }}>
      <div className="tabs">{children}</div>
    </TabsContext.Provider>
  );
}

function TabList({ children }) {
  return <div className="tab-list">{children}</div>;
}

function Tab({ id, children }) {
  const { activeTab, setActiveTab } = useContext(TabsContext);
  return (
    <button
      className={activeTab === id ? 'active' : ''}
      onClick={() => setActiveTab(id)}
    >
      {children}
    </button>
  );
}

function TabPanel({ id, children }) {
  const { activeTab } = useContext(TabsContext);
  return activeTab === id ? <div>{children}</div> : null;
}

// Usage
function Settings() {
  return (
    <Tabs defaultTab="general">
      <TabList>
        <Tab id="general">General</Tab>
        <Tab id="security">Security</Tab>
      </TabList>
      <TabPanel id="general">
        <GeneralSettings />
      </TabPanel>
      <TabPanel id="security">
        <SecuritySettings />
      </TabPanel>
    </Tabs>
  );
}

// ============================================
// 5. CONTAINER/PRESENTATIONAL PATTERN
// ============================================

// Presentational component (UI only)
function UserListView({ users, onDelete }) {
  return (
    <ul>
      {users.map(user => (
        <li key={user.id}>
          {user.name}
          <button onClick={() => onDelete(user.id)}>Delete</button>
        </li>
      ))}
    </ul>
  );
}

// Container component (logic)
function UserListContainer() {
  const [users, setUsers] = useState([]);

  useEffect(() => {
    fetchUsers().then(setUsers);
  }, []);

  const handleDelete = async (id) => {
    await deleteUser(id);
    setUsers(users.filter(u => u.id !== id));
  };

  return <UserListView users={users} onDelete={handleDelete} />;
}

// ============================================
// 6. HIGHER-ORDER COMPONENT (HOC) PATTERN
// ============================================

// HOC for authentication
function withAuth(Component) {
  return function AuthenticatedComponent(props) {
    const { user, loading } = useAuth();

    if (loading) return <Spinner />;
    if (!user) return <Redirect to="/login" />;

    return <Component {...props} user={user} />;
  };
}

// Usage
function Dashboard({ user }) {
  return <div>Welcome {user.name}</div>;
}

export default withAuth(Dashboard);

// ============================================
// 7. CONTROLLED COMPONENT PATTERN
// ============================================

function SearchForm() {
  const [query, setQuery] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    search(query);
  };

  return (
    <form onSubmit={handleSubmit}>
      <input
        type="text"
        value={query}  // Controlled by state
        onChange={(e) => setQuery(e.target.value)}
      />
      <button type="submit">Search</button>
    </form>
  );
}

// ============================================
// 8. UNCONTROLLED COMPONENT PATTERN
// ============================================

function FileUpload() {
  const fileInputRef = useRef(null);

  const handleSubmit = (e) => {
    e.preventDefault();
    const file = fileInputRef.current.files[0];  // Access DOM directly
    uploadFile(file);
  };

  return (
    <form onSubmit={handleSubmit}>
      <input type="file" ref={fileInputRef} />
      <button type="submit">Upload</button>
    </form>
  );
}
